import requests
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from data_processing import format_duration
from auth import is_authenticated, refresh_token_if_needed

# Reuse one HTTP connection pool across all GA4 calls (per-process) so we
# don't pay TLS handshake cost on every request. requests.post() under the
# hood opens a new connection each call - a Session keeps them warm.
_SESSION = requests.Session()

# Tiny per-token, in-process cache for the GA4 properties list. The
# /dashboard route hits this on EVERY load and the list changes maybe once
# a month - hammering Google for it on every page reload was pure waste.
# 5-minute TTL is short enough that a newly-granted property still shows
# up quickly without forcing a re-login.
_PROPS_CACHE = {}     # access_token -> (expires_at, (properties_list, error))
_PROPS_TTL = 300.0    # seconds

def get_ga4_properties(session):
    if not is_authenticated() or not refresh_token_if_needed():
        return []

    access_token = session.get('access_token') or ''
    now = time.time()
    cached = _PROPS_CACHE.get(access_token)
    if cached and cached[0] > now:
        return cached[1]

    try:
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }

        response = _SESSION.get(
            'https://analyticsadmin.googleapis.com/v1beta/accountSummaries',
            headers=headers, timeout=20
        )
        response.raise_for_status()
        summaries = response.json()

        properties_list = []
        for account in summaries.get('accountSummaries', []):
            for property in account.get('propertySummaries', []):
                properties_list.append({
                    'property_id': property['property'].split('/')[-1],
                    'display_name': property.get('displayName', 'Unnamed Property'),
                    'account_name': account.get('displayName', 'Unknown Account'),
                    'property_type': 'GA4'
                })

        _PROPS_CACHE[access_token] = (now + _PROPS_TTL, (properties_list, None))
        return properties_list, None
    except Exception as e:
        error_message = f"Error fetching GA4 properties: {str(e)}"
        print(error_message)
        return [], error_message

def get_property_name(access_token, property_id):
    """Get the display name of a GA4 property"""
    try:
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }

        response = requests.get(
            f'https://analyticsadmin.googleapis.com/v1beta/properties/{property_id}',
            headers=headers
        )
        response.raise_for_status()
        data = response.json()
        return data.get('displayName', property_id)
    except:
        return property_id

def get_ga4_overview(access_token, property_id, start_date, end_date):
    """Just the 8 headline overview metrics (lightweight) - used for comparison."""
    try:
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
            'X-TIMEZONE': 'UTC',
        }
        url = f'https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport'
        body = {
            "metrics": [
                {"name": "totalUsers"}, {"name": "newUsers"}, {"name": "activeUsers"},
                {"name": "sessions"}, {"name": "screenPageViews"}, {"name": "eventCount"},
                {"name": "bounceRate"}, {"name": "averageSessionDuration"},
            ],
            "dateRanges": [{
                "startDate": start_date.strftime('%Y-%m-%d'),
                "endDate": end_date.strftime('%Y-%m-%d')}],
        }
        r = requests.post(url, headers=headers, json=body, timeout=30)
        r.raise_for_status()
        data = r.json()
        if not data.get('rows'):
            return {}
        mv = data['rows'][0]['metricValues']
        total_users = int(mv[0]['value'])
        new_users = int(mv[1]['value'])
        return {
            'new_users': new_users,
            'active_users': int(mv[2]['value']),
            'returning_users': max(total_users - new_users, 0),
            'sessions': int(mv[3]['value']),
            'views': int(mv[4]['value']),
            'event_count': int(mv[5]['value']),
            'bounce_rate': round(float(mv[6]['value']) * 100, 1),
            'avg_engagement_seconds': round(float(mv[7]['value']), 1),
        }
    except Exception as e:
        print(f"Error fetching GA4 overview: {e}")
        return {}


def get_ga4_daily_overview(access_token, property_id, start_date, end_date):
    """Day-by-day series of the same 8 overview metrics get_ga4_overview
    returns as totals. Powers the GA4 Overview Infographic line charts.
    Returns a dict with one date list + one numeric series per metric, or
    {} on failure (chart layer falls back to empty)."""
    try:
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
            'X-TIMEZONE': 'UTC',
        }
        url = f'https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport'
        body = {
            "dimensions": [{"name": "date"}],
            "metrics": [
                {"name": "totalUsers"}, {"name": "newUsers"}, {"name": "activeUsers"},
                {"name": "sessions"}, {"name": "screenPageViews"}, {"name": "eventCount"},
                {"name": "bounceRate"}, {"name": "averageSessionDuration"},
            ],
            "dateRanges": [{
                "startDate": start_date.strftime('%Y-%m-%d'),
                "endDate": end_date.strftime('%Y-%m-%d')}],
            "orderBys": [{"dimension": {"dimensionName": "date"}}],
            "limit": 1000,
        }
        r = requests.post(url, headers=headers, json=body, timeout=30)
        r.raise_for_status()
        data = r.json()
        rows = data.get('rows', [])
        out = {
            'dates': [],
            'new_users': [], 'active_users': [], 'returning_users': [], 'sessions': [],
            'views': [], 'event_count': [], 'bounce_rate': [], 'avg_engagement_per_session': [],
        }
        for row in rows:
            d = row['dimensionValues'][0]['value']      # YYYYMMDD
            if len(d) == 8 and d.isdigit():
                date_str = f'{d[0:4]}-{d[4:6]}-{d[6:8]}'
            else:
                date_str = d
            mv = row['metricValues']
            total_users = int(mv[0]['value'])
            new_users = int(mv[1]['value'])
            out['dates'].append(date_str)
            out['new_users'].append(new_users)
            out['active_users'].append(int(mv[2]['value']))
            out['returning_users'].append(max(total_users - new_users, 0))
            out['sessions'].append(int(mv[3]['value']))
            out['views'].append(int(mv[4]['value']))
            out['event_count'].append(int(mv[5]['value']))
            out['bounce_rate'].append(round(float(mv[6]['value']) * 100, 1))
            out['avg_engagement_per_session'].append(round(float(mv[7]['value']), 1))
        return out
    except Exception as e:
        print(f"Error fetching GA4 daily overview: {e}")
        return {}


def get_ga4_extra(access_token, property_id, start_date, end_date):
    """Extra GA4 sections: Tech (device) + Landing Pages."""
    out = {'tech': [], 'landing': []}
    try:
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
            'X-TIMEZONE': 'UTC',
        }
        url = f'https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport'
        dr = [{"startDate": start_date.strftime('%Y-%m-%d'), "endDate": end_date.strftime('%Y-%m-%d')}]

        # Device and landing-page reports are independent - run them in parallel.
        _dev_body = {
            "dimensions": [{"name": "deviceCategory"}],
            "metrics": [{"name": "activeUsers"}, {"name": "sessions"}, {"name": "screenPageViews"}],
            "dateRanges": dr,
            "orderBys": [{"metric": {"metricName": "activeUsers"}, "desc": True}],
            "limit": 10}
        _lp_body = {
            "dimensions": [{"name": "landingPage"}],
            "metrics": [{"name": "sessions"}, {"name": "activeUsers"}, {"name": "screenPageViews"}],
            "dateRanges": dr,
            "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}],
            "limit": 15}
        with ThreadPoolExecutor(max_workers=2) as _ex:
            _f_dev = _ex.submit(_SESSION.post, url, headers=headers, json=_dev_body, timeout=30)
            _f_lp = _ex.submit(_SESSION.post, url, headers=headers, json=_lp_body, timeout=30)
            dev = _f_dev.result().json()
            lp_response = _f_lp.result()
        for row in dev.get('rows', []):
            mv = row['metricValues']
            out['tech'].append({'device': row['dimensionValues'][0]['value'] or '(not set)',
                                'users': int(mv[0]['value']), 'sessions': int(mv[1]['value']), 'views': int(mv[2]['value'])})

        lp = lp_response.json()
        for row in lp.get('rows', []):
            mv = row['metricValues']
            out['landing'].append({'page': row['dimensionValues'][0]['value'] or '/',
                                   'sessions': int(mv[0]['value']), 'users': int(mv[1]['value']), 'views': int(mv[2]['value'])})
    except Exception as e:
        print(f"Error fetching GA4 extra: {e}")
    return out


def get_ga4_acquisition(access_token, property_id, start_date, end_date, prev_start=None, prev_end=None):
    """User Acquisition by channel, with optional previous-period comparison.

    Returns a list: [{'channel', 'current': {...}, 'previous': {...}, 'change': {...}}]
    metrics per period: total_users, new_users, returning_users, sessions, event_count.
    """
    try:
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
            'X-TIMEZONE': 'UTC',
        }
        url = f'https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport'
        date_ranges = [{"startDate": start_date.strftime('%Y-%m-%d'), "endDate": end_date.strftime('%Y-%m-%d')}]
        if prev_start and prev_end:
            date_ranges.append({"startDate": prev_start.strftime('%Y-%m-%d'), "endDate": prev_end.strftime('%Y-%m-%d')})
        body = {
            "dimensions": [{"name": "firstUserPrimaryChannelGroup"}],
            "metrics": [{"name": "totalUsers"}, {"name": "newUsers"}, {"name": "sessions"}, {"name": "eventCount"}],
            "dateRanges": date_ranges,
            "orderBys": [{"metric": {"metricName": "totalUsers"}, "desc": True}],
            "limit": 25,
        }
        r = requests.post(url, headers=headers, json=body, timeout=30)
        r.raise_for_status()
        data = r.json()

        channels = {}
        for row in data.get('rows', []):
            dvs = row['dimensionValues']
            channel = dvs[0]['value']
            dr = dvs[1]['value'] if len(dvs) > 1 else 'date_range_0'
            mv = row['metricValues']
            tu, nu = int(mv[0]['value']), int(mv[1]['value'])
            rec = {'total_users': tu, 'new_users': nu, 'returning_users': max(tu - nu, 0),
                   'sessions': int(mv[2]['value']), 'event_count': int(mv[3]['value'])}
            period = 'current' if dr == 'date_range_0' else 'previous'
            channels.setdefault(channel, {})[period] = rec

        def _chg(cur, prev):
            try:
                return round((cur - prev) / prev * 100, 1) if prev else None
            except (TypeError, ZeroDivisionError):
                return None

        result = []
        for ch, rec in channels.items():
            cur = rec.get('current', {})
            prev = rec.get('previous', {})
            change = {}
            if prev:
                for k in ['total_users', 'new_users', 'returning_users', 'sessions', 'event_count']:
                    change[k] = _chg(cur.get(k, 0), prev.get(k, 0))
            result.append({'channel': ch, 'current': cur, 'previous': prev, 'change': change})
        result.sort(key=lambda x: x['current'].get('total_users', 0), reverse=True)
        return result
    except Exception as e:
        print(f"Error fetching GA4 acquisition: {e}")
        return []


def get_ga4_data(access_token, property_id, start_date, end_date):
    try:
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
            'X-TIMEZONE': 'UTC'
        }

        url = f'https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport'

        # Overall metrics request
        overall_request_body = {
            "dimensions": [
                {"name": "date"}
            ],
            "metrics": [
                {"name": "totalUsers"},
                {"name": "newUsers"},
                {"name": "activeUsers"},
                {"name": "eventCount"},
                {"name": "screenPageViews"},
                {"name": "userEngagementDuration"}
            ],
            "dateRanges": [{
                "startDate": start_date.strftime('%Y-%m-%d'),
                "endDate": end_date.strftime('%Y-%m-%d')
            }],
            "returnPropertyQuota": True
        }

        # Page metrics request
        page_request_body = {
            "dimensions": [
                {"name": "pagePath"}
            ],
            "metrics": [
                {"name": "screenPageViews"},
                {"name": "activeUsers"},
                {"name": "userEngagementDuration"}
            ],
            "dateRanges": [{
                "startDate": start_date.strftime('%Y-%m-%d'),
                "endDate": end_date.strftime('%Y-%m-%d')
            }],
            "limit": 10000
        }

        # Country demographics request
        country_request_body = {
            "dimensions": [
                {"name": "country"}
            ],
            "metrics": [
                {"name": "activeUsers"},
                {"name": "newUsers"},
                {"name": "engagedSessions"},
                {"name": "engagementRate"},
                {"name": "userEngagementDuration"}
            ],
            "dateRanges": [{
                "startDate": start_date.strftime('%Y-%m-%d'),
                "endDate": end_date.strftime('%Y-%m-%d')
            }],
            "orderBys": [
                {
                    "metric": {"metricName": "activeUsers"},
                    "desc": True
                }
            ],
            "limit": 250
        }

        # Event metrics request
        event_request_body = {
            "dimensions": [
                {"name": "eventName"}
            ],
            "metrics": [
                {"name": "eventCount"},
                {"name": "eventCountPerUser"}
            ],
            "dateRanges": [{
                "startDate": start_date.strftime('%Y-%m-%d'),
                "endDate": end_date.strftime('%Y-%m-%d')
            }],
            "orderBys": [{
                "metric": {"metricName": "eventCount"},
                "desc": True
            }],
            "limit": 10000
        }

        # Channel acquisition request
        channel_request_body = {
            "dimensions": [
                {"name": "firstUserSourceMedium"}
            ],
            "metrics": [
                {"name": "newUsers"},
                {"name": "sessionsPerUser"},
                {"name": "userEngagementDuration"}
            ],
            "dateRanges": [{
                "startDate": start_date.strftime('%Y-%m-%d'),
                "endDate": end_date.strftime('%Y-%m-%d')
            }],
            "orderBys": [{
                "metric": {"metricName": "newUsers"},
                "desc": True
            }],
            "limit": 2500
        }

        # Overview summary (NO date dimension -> accurate totals incl. ratio metrics)
        overview_request_body = {
            "metrics": [
                {"name": "totalUsers"},
                {"name": "newUsers"},
                {"name": "activeUsers"},
                {"name": "sessions"},
                {"name": "screenPageViews"},
                {"name": "eventCount"},
                {"name": "bounceRate"},
                {"name": "averageSessionDuration"},
                {"name": "userEngagementDuration"},
            ],
            "dateRanges": [{
                "startDate": start_date.strftime('%Y-%m-%d'),
                "endDate": end_date.strftime('%Y-%m-%d')
            }],
        }

        # The 6 GA4 runReport calls are completely independent - fire them in
        # parallel instead of one after another. This single change cuts the
        # combined-data page wait by 4-6x because every call previously
        # sat behind every other call's network round-trip.
        def _post(body):
            return _SESSION.post(url, headers=headers, json=body, timeout=30)

        with ThreadPoolExecutor(max_workers=6) as _ex:
            _f_overall = _ex.submit(_post, overall_request_body)
            _f_page = _ex.submit(_post, page_request_body)
            _f_country = _ex.submit(_post, country_request_body)
            _f_event = _ex.submit(_post, event_request_body)
            _f_channel = _ex.submit(_post, channel_request_body)
            _f_overview = _ex.submit(_post, overview_request_body)
            overall_response = _f_overall.result()
            page_response = _f_page.result()
            country_response = _f_country.result()
            event_response = _f_event.result()
            channel_response = _f_channel.result()
            overview_response = _f_overview.result()

        # Check responses
        for response in [overall_response, page_response, country_response, event_response, channel_response, overview_response]:
            response.raise_for_status()

        # Parse responses
        overall_data = overall_response.json()
        page_data = page_response.json()
        country_data = country_response.json()
        event_data = event_response.json()
        channel_data = channel_response.json()

        # Process data
        processed_data = {
            'headers': ['Date', 'Page Views', 'Users', 'Avg. Session Duration'],
            'rows': [],
            'totals': {
                'totalActiveUsers': 0,
                'totalNewUsers': 0,
                'totalEventCount': 0,
                'totalViews': 0,
                'totalEngagementTime': 0
            },
            'pageMetrics': [],
            'countryMetrics': [],
            'eventMetrics': [],
            'channelMetrics': [],
            'overview': {}
        }

        # Process overview summary (the 8 headline metrics)
        ov = overview_response.json()
        if ov.get('rows'):
            mv = ov['rows'][0]['metricValues']
            total_users = int(mv[0]['value'])
            new_users = int(mv[1]['value'])
            active_users = int(mv[2]['value'])
            sessions = int(mv[3]['value'])
            views = int(mv[4]['value'])
            events = int(mv[5]['value'])
            bounce = float(mv[6]['value'])          # 0..1
            avg_sess_dur = float(mv[7]['value'])     # seconds
            processed_data['overview'] = {
                'new_users': new_users,
                'active_users': active_users,
                'returning_users': max(total_users - new_users, 0),
                'event_count': events,
                'sessions': sessions,
                'bounce_rate': round(bounce * 100, 1),               # percent
                'avg_engagement_per_session': format_duration(avg_sess_dur),
                'avg_engagement_seconds': round(avg_sess_dur, 1),
                'views': views,
                'total_users': total_users,
            }

        # Process overall metrics
        if 'rows' in overall_data:
            for row in overall_data['rows']:
                date_val = row['dimensionValues'][0]['value']
                metrics = row['metricValues']

                formatted_date = datetime.strptime(date_val, '%Y%m%d').strftime('%Y-%m-%d')
                page_views = int(metrics[4]['value'])
                users = int(metrics[2]['value'])  # activeUsers
                duration = float(metrics[5]['value'])  # userEngagementDuration

                processed_data['rows'].append([
                    formatted_date,
                    page_views,
                    users,
                    format_duration(duration)
                ])

                # Update totals
                processed_data['totals']['totalActiveUsers'] += users
                processed_data['totals']['totalNewUsers'] += int(metrics[1]['value'])
                processed_data['totals']['totalEventCount'] += int(metrics[3]['value'])
                processed_data['totals']['totalViews'] += page_views
                processed_data['totals']['totalEngagementTime'] += duration

        # Process page metrics
        if 'rows' in page_data:
            for row in page_data['rows']:
                processed_data['pageMetrics'].append({
                    'page': row['dimensionValues'][0]['value'],
                    'views': int(row['metricValues'][0]['value']),
                    'users': int(row['metricValues'][1]['value']),
                    'avgDuration': format_duration(float(row['metricValues'][2]['value']))
                })

        # Process country metrics
        if 'rows' in country_data:
            for row in country_data['rows']:
                processed_data['countryMetrics'].append({
                    'country': row['dimensionValues'][0]['value'],
                    'users': int(row['metricValues'][0]['value']),
                    'newUsers': int(row['metricValues'][1]['value']),
                    'engagedSessions': int(row['metricValues'][2]['value']),
                    'engagementRate': f"{float(row['metricValues'][3]['value']) * 100:.1f}%"
                })

        # Process event metrics
        if 'rows' in event_data:
            for row in event_data['rows']:
                processed_data['eventMetrics'].append({
                    'event': row['dimensionValues'][0]['value'],
                    'count': int(row['metricValues'][0]['value']),
                    'perUser': float(row['metricValues'][1]['value'])
                })

        # Process channel metrics
        if 'rows' in channel_data:
            total_new_users = sum(float(row['metricValues'][0]['value']) for row in channel_data['rows'])

            for row in channel_data['rows']:
                new_users = float(row['metricValues'][0]['value'])
                processed_data['channelMetrics'].append({
                    'source': row['dimensionValues'][0]['value'],
                    'newUsers': int(new_users),
                    'percentage': f"{(new_users / total_new_users * 100):.1f}%",
                    'sessionsPerUser': float(row['metricValues'][1]['value']),
                    'avgEngagementTime': format_duration(float(row['metricValues'][2]['value']))
                })

        return processed_data

    except Exception as e:
        print(f"Error fetching GA4 data: {e}")
        return None
