"""Supabase (Postgres via PostgREST) helpers for the PMO portal.

Uses plain HTTPS (port 443) so it works on Render. Reads two env vars:
  SUPABASE_URL  -> e.g. https://abcd1234.supabase.co
  SUPABASE_KEY  -> the *service_role* secret key (kept server-side only)

Every function degrades gracefully: if Supabase is not configured, reads
return empty results and writes are skipped, so the rest of the app keeps
working (e.g. emailing a report still succeeds even without logging).
"""
import os
import requests

TIMEOUT = 20


def _config():
    url = (os.getenv('SUPABASE_URL') or '').rstrip('/')
    key = os.getenv('SUPABASE_KEY')
    return url, key


def is_configured():
    url, key = _config()
    return bool(url and key)


def _headers(extra=None):
    _, key = _config()
    h = {
        'apikey': key,
        'Authorization': f'Bearer {key}',
        'Content-Type': 'application/json',
    }
    if extra:
        h.update(extra)
    return h


def _rest(path):
    url, _ = _config()
    return f"{url}/rest/v1/{path}"


# ---------------- Clients ----------------
def list_clients():
    if not is_configured():
        return []
    r = requests.get(_rest('clients?select=*&order=created_at.desc'),
                     headers=_headers(), timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def get_client(client_id):
    if not is_configured():
        return None
    r = requests.get(_rest(f'clients?id=eq.{client_id}&select=*'),
                     headers=_headers(), timeout=TIMEOUT)
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None


def add_client(data):
    r = requests.post(_rest('clients'),
                      headers=_headers({'Prefer': 'return=representation'}),
                      json=data, timeout=TIMEOUT)
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None


def update_client(client_id, data):
    r = requests.patch(_rest(f'clients?id=eq.{client_id}'),
                       headers=_headers({'Prefer': 'return=representation'}),
                       json=data, timeout=TIMEOUT)
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None


def delete_client(client_id):
    r = requests.delete(_rest(f'clients?id=eq.{client_id}'),
                        headers=_headers(), timeout=TIMEOUT)
    r.raise_for_status()
    return True


# ---------------- Report logs ----------------
def log_report(client_id, report_period, sent_to, subject, status='sent'):
    """Record that a report email was sent. Never raises - logging is best-effort."""
    if not is_configured() or not client_id:
        return None
    try:
        entry = {
            'client_id': client_id,
            'report_period': report_period,
            'sent_to': sent_to,
            'subject': subject,
            'status': status,
        }
        r = requests.post(_rest('report_logs'),
                          headers=_headers({'Prefer': 'return=representation'}),
                          json=entry, timeout=TIMEOUT)
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else None
    except Exception as e:
        print(f'[db] log_report failed (non-fatal): {e}')
        return None


# ---------------- Activity feed (notification bell) ----------------
def log_activity(activity_type, message, link=None, user_email=None):
    """Record an activity for the notification bell. Best-effort (never raises)."""
    if not is_configured():
        return None
    try:
        entry = {
            'type': activity_type,
            'message': message,
            'link': link,
            'user_email': user_email,
        }
        r = requests.post(_rest('activities'),
                          headers=_headers({'Prefer': 'return=representation'}),
                          json=entry, timeout=TIMEOUT)
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else None
    except Exception as e:
        print(f'[db] log_activity failed (non-fatal): {e}')
        return None


def list_activities(limit=20):
    """Most recent activities, newest first, for the notification bell."""
    if not is_configured():
        return []
    try:
        r = requests.get(_rest(f'activities?select=*&order=created_at.desc&limit={limit}'),
                         headers=_headers(), timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f'[db] list_activities failed (non-fatal): {e}')
        return []


def latest_reports():
    """Return {client_id: latest_report_log} so the portal can show the most
    recent report sent to each client."""
    if not is_configured():
        return {}
    r = requests.get(_rest('report_logs?select=*&order=sent_at.desc'),
                     headers=_headers(), timeout=TIMEOUT)
    r.raise_for_status()
    latest = {}
    for log in r.json():
        cid = log.get('client_id')
        if cid and cid not in latest:
            latest[cid] = log
    return latest
