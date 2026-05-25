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
from datetime import datetime, timezone

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


def client_reports(client_id):
    """All report_logs for one client, newest first (for the client profile page)."""
    if not is_configured() or not client_id:
        return []
    try:
        r = requests.get(_rest(f'report_logs?client_id=eq.{client_id}&select=*&order=sent_at.desc'),
                         headers=_headers(), timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f'[db] client_reports failed (non-fatal): {e}')
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


# ---------------- Client lookup (for the client portal) ----------------
def get_client_by_email(email):
    """Find the client whose email matches the given address (case-insensitive).
    Used to recognise a portal visitor as a client. Returns the row or None."""
    if not is_configured() or not email:
        return None
    try:
        e = email.strip().lower()
        r = requests.get(_rest(f'clients?email=ilike.{e}&select=*'),
                         headers=_headers(), timeout=TIMEOUT)
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else None
    except Exception as e:
        print(f'[db] get_client_by_email failed (non-fatal): {e}')
        return None


# ---------------- Client queries ----------------
def add_query(data):
    """Insert a query raised by a client from the portal. Returns the new row."""
    if not is_configured():
        return None
    r = requests.post(_rest('report_queries'),
                      headers=_headers({'Prefer': 'return=representation'}),
                      json=data, timeout=TIMEOUT)
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None


def client_queries(client_id):
    """All queries raised by one client, newest first (client portal)."""
    if not is_configured() or not client_id:
        return []
    try:
        r = requests.get(_rest(f'report_queries?client_id=eq.{client_id}&select=*&order=created_at.desc'),
                         headers=_headers(), timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f'[db] client_queries failed (non-fatal): {e}')
        return []


def list_queries(status=None):
    """Every query (newest first) with the client's name/email embedded, for the
    PMO queries dashboard. Optionally filter by status (open/answered/resolved)."""
    if not is_configured():
        return []
    try:
        q = 'report_queries?select=*,client:clients(name,email)&order=created_at.desc'
        if status:
            q += f'&status=eq.{status}'
        r = requests.get(_rest(q), headers=_headers(), timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f'[db] list_queries failed (non-fatal): {e}')
        return []


def respond_query(query_id, response, status, responded_by):
    """PMO answers a query: store the response, status, and who/when."""
    if not is_configured() or not query_id:
        return None
    data = {
        'response': response,
        'status': status or 'answered',
        'responded_by': responded_by,
        'responded_at': datetime.now(timezone.utc).isoformat(),
    }
    r = requests.patch(_rest(f'report_queries?id=eq.{query_id}'),
                       headers=_headers({'Prefer': 'return=representation'}),
                       json=data, timeout=TIMEOUT)
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None


def count_open_queries():
    """Number of queries still awaiting a response (for the PMO badge)."""
    if not is_configured():
        return 0
    try:
        r = requests.get(_rest('report_queries?status=eq.open&select=id'),
                         headers=_headers(), timeout=TIMEOUT)
        r.raise_for_status()
        return len(r.json())
    except Exception as e:
        print(f'[db] count_open_queries failed (non-fatal): {e}')
        return 0
