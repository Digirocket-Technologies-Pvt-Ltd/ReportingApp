"""Google Merchant Center (GMC) helper.

Workflow (as the agency works): the logged-in account may have access to MANY
merchant accounts (a multi-client / aggregator account). For a given website we
first CHECK whether a Merchant Center account exists for that domain:
  - if one exists  -> return its data (products + shopping performance)
  - if none exists -> return {} so NO GMC section is shown.

Prerequisites (one-time): enable "Content API for Shopping" in Google Cloud and
the OAuth scope `https://www.googleapis.com/auth/content` (added to config) -> the
user must re-login once to grant it. All calls are best-effort.
"""
import requests

BASE = 'https://shoppingcontent.googleapis.com/content/v2.1'


def _domain(url):
    u = (url or '').replace('sc-domain:', '').replace('https://', '').replace('http://', '').strip('/')
    return u.split('/')[0].lower()


def _candidate_accounts(access_token, headers):
    """All merchant accounts the user can access: [(merchant_id, account_resource)]."""
    out = []
    info = requests.get(f'{BASE}/accounts/authinfo', headers=headers, timeout=30).json()
    for ident in info.get('accountIdentifiers', []):
        if ident.get('merchantId'):
            mid = ident['merchantId']
            try:
                acct = requests.get(f'{BASE}/{mid}/accounts/{mid}', headers=headers, timeout=30).json()
                out.append((mid, acct))
            except Exception:
                pass
        elif ident.get('aggregatorId'):
            agg = ident['aggregatorId']
            token = None
            for _ in range(10):  # page through sub-accounts
                params = {'maxResults': 250}
                if token:
                    params['pageToken'] = token
                subs = requests.get(f'{BASE}/{agg}/accounts', headers=headers, params=params, timeout=30).json()
                for sub in subs.get('resources', []):
                    out.append((sub.get('id'), sub))
                token = subs.get('nextPageToken')
                if not token:
                    break
    return out


def get_gmc_data(access_token, site_url, start_date, end_date):
    """Return GMC data for the merchant account matching `site_url`, or {} if none."""
    headers = {'Authorization': f'Bearer {access_token}'}
    try:
        domain = _domain(site_url)
        if not domain:
            return {}

        merchant_id, merchant_name, website = None, None, None
        for mid, acct in _candidate_accounts(access_token, headers):
            wurl = (acct.get('websiteUrl') or '').lower()
            if domain and domain in wurl:
                merchant_id, merchant_name, website = mid, acct.get('name'), acct.get('websiteUrl')
                break

        if not merchant_id:
            return {}  # no Merchant Center account for this website -> no GMC section

        result = {'merchant_name': merchant_name or merchant_id, 'website': website, 'merchant_id': merchant_id}

        # Shopping performance (clicks / impressions) for the date range
        try:
            q = ("SELECT metrics.clicks, metrics.impressions FROM MerchantPerformanceView "
                 f"WHERE segments.date BETWEEN '{start_date.strftime('%Y-%m-%d')}' "
                 f"AND '{end_date.strftime('%Y-%m-%d')}'")
            rep = requests.post(f'{BASE}/{merchant_id}/reports/search', headers=headers,
                                json={'query': q}, timeout=30).json()
            clicks = impr = 0
            for row in rep.get('results', []):
                m = row.get('metrics', {}) or {}
                clicks += int(m.get('clicks', 0) or 0)
                impr += int(m.get('impressions', 0) or 0)
            result['clicks'] = clicks
            result['impressions'] = impr
        except Exception as e:
            print(f"GMC performance error: {e}")

        # Approx active product count (first page)
        try:
            prods = requests.get(f'{BASE}/{merchant_id}/products', headers=headers,
                                 params={'maxResults': 250}, timeout=30).json()
            n = len(prods.get('resources', []))
            result['products'] = f"{n}+" if prods.get('nextPageToken') else n
        except Exception:
            pass

        return result
    except Exception as e:
        print(f"GMC error: {e}")
        return {}
