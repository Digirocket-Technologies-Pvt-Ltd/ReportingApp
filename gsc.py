import time
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from datetime import datetime
from urllib.parse import unquote
from concurrent.futures import ThreadPoolExecutor
from auth import is_authenticated, refresh_token_if_needed
from config import CLIENT_ID, CLIENT_SECRET, SCOPES

# Per-token, in-process cache for the GSC sites list (same rationale as the
# GA4 properties cache: dashboard hits this on every load and the list
# rarely changes).
_SITES_CACHE = {}     # access_token -> (expires_at, sites_list)
_SITES_TTL = 300.0    # seconds


def normalize_gsc_property(site_url):
    """Return a valid Search Console siteUrl from whatever the user pasted.

    Handles the common mistake of pasting the GSC UI URL fragment, e.g.
    'resource_id=https%3A%2F%2Fexample.com%2F' -> 'https://example.com/'.
    Valid values ('https://example.com/', 'sc-domain:example.com') pass through.
    """
    if not site_url:
        return site_url
    s = str(site_url).strip()
    if 'resource_id=' in s:
        s = s.split('resource_id=', 1)[1].split('&', 1)[0]
    # decode percent-encoding (twice in case it was double-encoded)
    for _ in range(2):
        if '%' in s:
            s = unquote(s)
        else:
            break
    return s.strip()

def get_gsc_sites(session):
    if not is_authenticated() or not refresh_token_if_needed():
        return []

    access_token = session.get('access_token') or ''
    now = time.time()
    cached = _SITES_CACHE.get(access_token)
    if cached and cached[0] > now:
        return cached[1]

    try:
        credentials = Credentials(
            token=access_token,
            refresh_token=session.get('refresh_token'),
            token_uri='https://oauth2.googleapis.com/token',
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            scopes=SCOPES
        )

        webmasters_service = build('searchconsole', 'v1', credentials=credentials,
                                   cache_discovery=False)
        sites = webmasters_service.sites().list().execute()

        sites_list = []
        for site in sites.get('siteEntry', []):
            sites_list.append({
                'site_url': site['siteUrl'],
                'permission_level': site.get('permissionLevel', 'Unknown')
            })

        _SITES_CACHE[access_token] = (now + _SITES_TTL, sites_list)
        return sites_list
    except Exception as e:
        print(f"Error fetching GSC sites: {e}")
        return []

def get_gsc_summary(credentials, site_url, start_date, end_date):
    """Just totals (clicks, impressions, ctr, position) - lightweight, for comparison."""
    try:
        site_url = normalize_gsc_property(site_url)
        ws = build('searchconsole', 'v1', credentials=credentials)
        req = {
            'startDate': start_date.strftime('%Y-%m-%d'),
            'endDate': end_date.strftime('%Y-%m-%d'),
            'dimensions': ['date'],
            'rowLimit': 1000,
        }
        resp = ws.searchanalytics().query(siteUrl=site_url, body=req).execute()
        rows = resp.get('rows', [])
        clicks = sum(r['clicks'] for r in rows)
        impr = sum(r['impressions'] for r in rows)
        ctr = (clicks / impr * 100) if impr else 0
        pos = sum(r['position'] for r in rows) / len(rows) if rows else 0
        return {'clicks': int(clicks), 'impressions': int(impr),
                'ctr': round(ctr, 2), 'position': round(pos, 1)}
    except Exception as e:
        print(f"Error fetching GSC summary: {e}")
        return {}


def get_gsc_detailed_data(credentials, site_url, start_date, end_date):
    """
    Get detailed Google Search Console data including date metrics, top queries,
    top pages, country data, and device data.
    """
    try:
        site_url = normalize_gsc_property(site_url)

        # Query for date-based metrics
        # rowLimit must be large enough to cover long ranges (e.g. 3 months = ~92 days),
        # otherwise the totals get truncated and under-count. 50 was a bug for long ranges.
        date_request = {
            'startDate': start_date.strftime('%Y-%m-%d'),
            'endDate': end_date.strftime('%Y-%m-%d'),
            'dimensions': ['date'],
            'rowLimit': 1000
        }
        # Query for top queries
        top_queries_request = {
            'startDate': start_date.strftime('%Y-%m-%d'),
            'endDate': end_date.strftime('%Y-%m-%d'),
            'dimensions': ['query'],
            'rowLimit': 10,
            'orderBy': [
                {'dimension': None, 'field': 'clicks', 'sortOrder': 'DESCENDING'}
            ]
        }

        # Query for top pages
        top_pages_request = {
            'startDate': start_date.strftime('%Y-%m-%d'),
            'endDate': end_date.strftime('%Y-%m-%d'),
            'dimensions': ['page'],
            'rowLimit': 10,
            'orderBy': [
                {'dimension': None, 'field': 'clicks', 'sortOrder': 'DESCENDING'}
            ]
        }

        # Query for country data
        country_request = {
            'startDate': start_date.strftime('%Y-%m-%d'),
            'endDate': end_date.strftime('%Y-%m-%d'),
            'dimensions': ['country'],
            'rowLimit': 200,
            'orderBy': [
                {'dimension': None, 'field': 'clicks', 'sortOrder': 'DESCENDING'}
            ]
        }

        # Query for device data
        device_request = {
            'startDate': start_date.strftime('%Y-%m-%d'),
            'endDate': end_date.strftime('%Y-%m-%d'),
            'dimensions': ['device'],
            'rowLimit': 10,
            'orderBy': [
                {'dimension': None, 'field': 'clicks', 'sortOrder': 'DESCENDING'}
            ]
        }

        # All 5 GSC queries are independent - parallelise so the wall-clock
        # wait drops to roughly the slowest single query instead of the sum.
        # Each call uses its own short-lived service client because Google's
        # discovery client isn't documented as thread-safe across .execute().
        def _gsc_query(body):
            ws = build('searchconsole', 'v1', credentials=credentials,
                       cache_discovery=False)
            return ws.searchanalytics().query(siteUrl=site_url, body=body).execute()

        with ThreadPoolExecutor(max_workers=5) as _ex:
            _f_date = _ex.submit(_gsc_query, date_request)
            _f_q = _ex.submit(_gsc_query, top_queries_request)
            _f_p = _ex.submit(_gsc_query, top_pages_request)
            _f_c = _ex.submit(_gsc_query, country_request)
            _f_d = _ex.submit(_gsc_query, device_request)
            date_response = _f_date.result()
            top_queries_response = _f_q.result()
            top_pages_response = _f_p.result()
            country_response = _f_c.result()
            device_response = _f_d.result()

        # Process the responses
        rows = date_response.get('rows', [])

        # Calculate summary statistics
        total_clicks = sum(row['clicks'] for row in rows)
        total_impressions = sum(row['impressions'] for row in rows)
        average_ctr = (total_clicks / total_impressions * 100) if total_impressions > 0 else 0
        average_position = sum(row['position'] for row in rows) / len(rows) if rows else 0

        processed_data = {
            'daily_metrics': {
                'headers': ['Date', 'Clicks', 'Impressions', 'CTR', 'Position'],
                'rows': [[
                    datetime.strptime(row['keys'][0], '%Y-%m-%d').strftime('%Y-%m-%d'),
                    int(row['clicks']),
                    int(row['impressions']),
                    f"{(row['clicks'] / row['impressions'] * 100):.2f}%" if row['impressions'] > 0 else "0%",
                    f"{row['position']:.1f}"
                ] for row in rows]
            },
            'top_queries': {
                'headers': ['Query', 'Clicks', 'Impressions', 'CTR', 'Position'],
                'rows': [[
                    row['keys'][0],
                    int(row['clicks']),
                    int(row['impressions']),
                    f"{(row['clicks'] / row['impressions'] * 100):.2f}%" if row['impressions'] > 0 else "0%",
                    f"{row['position']:.1f}"
                ] for row in top_queries_response.get('rows', [])]
            },
            'top_pages': {
                'headers': ['Page', 'Clicks', 'Impressions', 'CTR', 'Position'],
                'rows': [[
                    row['keys'][0],
                    int(row['clicks']),
                    int(row['impressions']),
                    f"{(row['clicks'] / row['impressions'] * 100):.2f}%" if row['impressions'] > 0 else "0%",
                    f"{row['position']:.1f}"
                ] for row in top_pages_response.get('rows', [])]
            },
            'country_data': {
                'headers': ['Country', 'Clicks', 'Impressions', 'CTR', 'Position'],
                'rows': [[
                    row['keys'][0],
                    int(row['clicks']),
                    int(row['impressions']),
                    f"{(row['clicks'] / row['impressions'] * 100):.2f}%" if row['impressions'] > 0 else "0%",
                    f"{row['position']:.1f}"
                ] for row in country_response.get('rows', [])]
            },
            'device_data': {
                'headers': ['Device', 'Clicks', 'Impressions', 'CTR', 'Position'],
                'rows': [[
                    row['keys'][0],
                    int(row['clicks']),
                    int(row['impressions']),
                    f"{(row['clicks'] / row['impressions'] * 100):.2f}%" if row['impressions'] > 0 else "0%",
                    f"{row['position']:.1f}"
                ] for row in device_response.get('rows', [])]
            },
            'summary': {
                'total_clicks': total_clicks,
                'total_impressions': total_impressions,
                'average_ctr': f"{average_ctr:.2f}%",
                'average_position': f"{average_position:.1f}"
            }
        }

        return processed_data

    except Exception as e:
        print(f"Error fetching GSC detailed data: {e}")
        return None
