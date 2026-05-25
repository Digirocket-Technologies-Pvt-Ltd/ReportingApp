"""Google Business Profile (GMB) helpers.

Fetches the business location + its performance metrics (calls, website clicks,
direction requests, bookings, conversations, impressions) for a date range.

Prerequisites (one-time, in Google Cloud Console for the OAuth project):
  - Enable: "Business Profile Performance API", "My Business Account Management API",
    "My Business Business Information API".
  - The OAuth scope `https://www.googleapis.com/auth/business.manage` (added to
    config.SCOPES) -> the user must re-login once to grant it.
  - The logged-in Google account must have access to the Business Profile.
All calls are best-effort: any failure returns empty so the rest of the report
keeps working.
"""
import requests

PERF_BASE = 'https://businessprofileperformance.googleapis.com/v1'
ACCT_BASE = 'https://mybusinessaccountmanagement.googleapis.com/v1'
INFO_BASE = 'https://mybusinessbusinessinformation.googleapis.com/v1'

_IMPRESSION_METRICS = [
    'BUSINESS_IMPRESSIONS_DESKTOP_MAPS', 'BUSINESS_IMPRESSIONS_DESKTOP_SEARCH',
    'BUSINESS_IMPRESSIONS_MOBILE_MAPS', 'BUSINESS_IMPRESSIONS_MOBILE_SEARCH',
]
_ALL_METRICS = ['CALL_CLICKS', 'WEBSITE_CLICKS', 'BUSINESS_DIRECTION_REQUESTS',
                'BUSINESS_BOOKINGS', 'BUSINESS_CONVERSATIONS'] + _IMPRESSION_METRICS


def get_gmb_location(access_token):
    """Return the first Business Profile location {'name': 'locations/123', 'title': ...}."""
    headers = {'Authorization': f'Bearer {access_token}'}
    try:
        accts = requests.get(f'{ACCT_BASE}/accounts', headers=headers, timeout=30).json()
        for acct in accts.get('accounts', []):
            acc_name = acct.get('name')  # accounts/123
            if not acc_name:
                continue
            locs = requests.get(
                f'{INFO_BASE}/{acc_name}/locations',
                headers=headers, params={'readMask': 'name,title', 'pageSize': 10}, timeout=30).json()
            for loc in locs.get('locations', []):
                return {'name': loc.get('name'), 'title': loc.get('title', '')}
        return None
    except Exception as e:
        print(f"GMB location error: {e}")
        return None


def get_gmb_performance(access_token, location_name, start_date, end_date):
    """Sum GMB metrics over the date range. location_name = 'locations/123'."""
    headers = {'Authorization': f'Bearer {access_token}'}
    params = [('dailyMetrics', m) for m in _ALL_METRICS]
    params += [
        ('dailyRange.start_date.year', start_date.year),
        ('dailyRange.start_date.month', start_date.month),
        ('dailyRange.start_date.day', start_date.day),
        ('dailyRange.end_date.year', end_date.year),
        ('dailyRange.end_date.month', end_date.month),
        ('dailyRange.end_date.day', end_date.day),
    ]
    try:
        url = f'{PERF_BASE}/{location_name}:fetchMultiDailyMetricsTimeSeries'
        r = requests.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        sums = {}
        for series in data.get('multiDailyMetricTimeSeries', []):
            for dm in series.get('dailyMetricTimeSeries', []):
                metric = dm.get('dailyMetric')
                total = 0
                for dv in (dm.get('timeSeries', {}) or {}).get('datedValues', []):
                    total += int(dv.get('value', 0) or 0)
                sums[metric] = sums.get(metric, 0) + total
        impressions = sum(sums.get(k, 0) for k in _IMPRESSION_METRICS)
        return {
            'calls': sums.get('CALL_CLICKS', 0),
            'website_clicks': sums.get('WEBSITE_CLICKS', 0),
            'directions': sums.get('BUSINESS_DIRECTION_REQUESTS', 0),
            'bookings': sums.get('BUSINESS_BOOKINGS', 0),
            'conversations': sums.get('BUSINESS_CONVERSATIONS', 0),
            'impressions': impressions,
        }
    except Exception as e:
        print(f"GMB performance error: {e}")
        return {}


def get_gmb_data(access_token, start_date, end_date):
    """Convenience: find the location and fetch its performance. Returns {} if unavailable."""
    loc = get_gmb_location(access_token)
    if not loc or not loc.get('name'):
        return {}
    perf = get_gmb_performance(access_token, loc['name'], start_date, end_date)
    if not perf:
        return {}
    perf['title'] = loc.get('title', '')
    return perf


def gmb_debug(access_token, start_date=None, end_date=None):
    """Step-by-step diagnostic of the GMB API chain. Returns a dict of raw results
    so we can see exactly where it breaks (accounts / locations / performance)."""
    headers = {'Authorization': f'Bearer {access_token}'}
    out = {'steps': []}

    # Step 1: list accounts
    try:
        r = requests.get(f'{ACCT_BASE}/accounts', headers=headers, timeout=30)
        body = r.json()
        accounts = body.get('accounts', [])
        out['steps'].append({
            'step': '1. List accounts (Account Management API)',
            'http_status': r.status_code,
            'account_count': len(accounts),
            'accounts': [{'name': a.get('name'), 'accountName': a.get('accountName'),
                          'type': a.get('type'), 'role': a.get('role')} for a in accounts],
            'error': body.get('error'),
            'raw': body if r.status_code != 200 else None,
        })
    except Exception as e:
        out['steps'].append({'step': '1. List accounts', 'exception': str(e)})
        return out

    if not accounts:
        out['conclusion'] = ("No Business Profile accounts found for this Google account. "
                             "This account does not manage any Google Business Profile, OR the "
                             "Account Management API is not enabled / scope not granted.")
        return out

    # Step 2: list locations for each account
    found_loc = None
    for acct in accounts:
        acc_name = acct.get('name')
        if not acc_name:
            continue
        try:
            r = requests.get(f'{INFO_BASE}/{acc_name}/locations', headers=headers,
                             params={'readMask': 'name,title', 'pageSize': 100}, timeout=30)
            body = r.json()
            locs = body.get('locations', [])
            out['steps'].append({
                'step': f'2. List locations for {acc_name} (Business Information API)',
                'http_status': r.status_code,
                'location_count': len(locs),
                'locations': [{'name': l.get('name'), 'title': l.get('title')} for l in locs],
                'error': body.get('error'),
                'raw': body if r.status_code != 200 else None,
            })
            if locs and not found_loc:
                found_loc = locs[0]
        except Exception as e:
            out['steps'].append({'step': f'2. List locations for {acc_name}', 'exception': str(e)})

    if not found_loc:
        out['conclusion'] = ("Accounts exist but NO locations returned. This account can see the "
                             "Business Profile account but has no managed business locations, OR the "
                             "Business Information API is not enabled.")
        return out

    # Step 3: fetch performance for the first location
    if start_date and end_date:
        params = [('dailyMetrics', m) for m in _ALL_METRICS]
        params += [
            ('dailyRange.start_date.year', start_date.year),
            ('dailyRange.start_date.month', start_date.month),
            ('dailyRange.start_date.day', start_date.day),
            ('dailyRange.end_date.year', end_date.year),
            ('dailyRange.end_date.month', end_date.month),
            ('dailyRange.end_date.day', end_date.day),
        ]
        try:
            url = f"{PERF_BASE}/{found_loc.get('name')}:fetchMultiDailyMetricsTimeSeries"
            r = requests.get(url, headers=headers, params=params, timeout=30)
            body = r.json()
            out['steps'].append({
                'step': f"3. Performance for {found_loc.get('name')} (Performance API)",
                'http_status': r.status_code,
                'error': body.get('error'),
                'raw': body,
            })
        except Exception as e:
            out['steps'].append({'step': '3. Performance', 'exception': str(e)})

    return out
