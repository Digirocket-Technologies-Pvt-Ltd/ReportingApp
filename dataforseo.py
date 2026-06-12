"""DataForSEO integration — domain SEO overview for a client.

Uses the DataForSEO Labs "Domain Rank Overview" endpoint to get, for a client's
website domain: estimated organic traffic + total ranked keywords + a breakdown
of keyword positions. One paid API call per fetch (cached on the client row).

Auth: HTTP Basic with DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD (from .env).
Get these from the DataForSEO dashboard -> API Access page.
"""
import os
import re
import requests

API_BASE = 'https://api.dataforseo.com/v3'
# Default market. 2840 = United States, 2356 = India. Override per call if needed.
DEFAULT_LOCATION = int(os.getenv('DATAFORSEO_LOCATION_CODE', '2840'))
DEFAULT_LANGUAGE = os.getenv('DATAFORSEO_LANGUAGE_CODE', 'en')


def is_configured():
    return bool(os.getenv('DATAFORSEO_LOGIN') and os.getenv('DATAFORSEO_PASSWORD'))


def clean_domain(url_or_domain):
    """Turn 'https://www.example.com/path' or 'sc-domain:example.com' into
    a bare 'example.com' that DataForSEO accepts as a target."""
    if not url_or_domain:
        return ''
    s = str(url_or_domain).strip().lower()
    s = s.replace('sc-domain:', '')
    s = re.sub(r'^https?://', '', s)
    s = s.split('/')[0]              # drop path
    s = s.split('?')[0]
    if s.startswith('www.'):
        s = s[4:]
    return s.strip()


def _auth():
    return (os.getenv('DATAFORSEO_LOGIN'), os.getenv('DATAFORSEO_PASSWORD'))


def domain_overview(domain, location_code=None, language_code=None):
    """Estimated organic traffic + ranked-keyword stats for a domain.
    Returns a dict (never raises) — {'error': ...} on failure."""
    if not is_configured():
        return {'error': 'DataForSEO not configured (set DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD).'}
    target = clean_domain(domain)
    if not target:
        return {'error': 'No website domain set for this client.'}
    payload = [{
        'target': target,
        'location_code': location_code or DEFAULT_LOCATION,
        'language_code': language_code or DEFAULT_LANGUAGE,
    }]
    try:
        r = requests.post(
            f'{API_BASE}/dataforseo_labs/google/domain_rank_overview/live',
            auth=_auth(), json=payload, timeout=60)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f'[dataforseo] request failed: {e}')
        return {'error': 'DataForSEO request failed.', 'target': target}

    try:
        task = (data.get('tasks') or [{}])[0]
        if task.get('status_code') != 20000:
            return {'error': task.get('status_message') or 'DataForSEO task error.',
                    'target': target}
        result = (task.get('result') or [{}])[0]
        # metrics live under result.items[0].metrics (organic / paid).
        item = ((result.get('items') or [{}])[0]) if result.get('items') else {}
        metrics = item.get('metrics') or {}
        org = metrics.get('organic') or {}
        paid = metrics.get('paid') or {}
        return {
            'target': target,
            'location_code': payload[0]['location_code'],
            'organic_keywords': org.get('count', 0),
            'organic_traffic': round(org.get('etv', 0) or 0),     # estimated monthly organic visits
            'pos_1': org.get('pos_1', 0),
            'pos_2_3': org.get('pos_2_3', 0),
            'pos_4_10': org.get('pos_4_10', 0),
            'pos_11_20': org.get('pos_11_20', 0),
            'pos_21_30': org.get('pos_21_30', 0),
            'paid_keywords': paid.get('count', 0),
            'cost': data.get('cost', 0),
        }
    except Exception as e:
        print(f'[dataforseo] parse failed: {e}')
        return {'error': 'Could not parse DataForSEO response.', 'target': target}
