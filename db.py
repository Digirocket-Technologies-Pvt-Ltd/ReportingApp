"""Supabase (Postgres via PostgREST) helpers for the PMO portal.

Uses plain HTTPS (port 443) so it works on Render. Reads two env vars:
  SUPABASE_URL  -> e.g. https://abcd1234.supabase.co
  SUPABASE_KEY  -> the *service_role* secret key (kept server-side only)

Every function degrades gracefully: if Supabase is not configured, reads
return empty results and writes are skipped, so the rest of the app keeps
working (e.g. emailing a report still succeeds even without logging).
"""
import os
import uuid
import requests
from datetime import datetime, timezone
from werkzeug.utils import secure_filename

TIMEOUT = 20
ATTACHMENT_BUCKET = 'query-attachments'
_bucket_ready = False  # cache: don't hit Supabase on every upload


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
def log_report(client_id, report_period, sent_to, subject, status='sent', files=None):
    """Record that a report email was sent. Optionally store the files (list of
    {name, url, size, type}) so the client portal can open them. Never raises."""
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
        if files:
            entry['files'] = files
        r = requests.post(_rest('report_logs'),
                          headers=_headers({'Prefer': 'return=representation'}),
                          json=entry, timeout=TIMEOUT)
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else None
    except Exception as e:
        print(f'[db] log_report failed (non-fatal): {e}')
        return None


def mark_report_viewed(report_id, client_id=None):
    """Stamp viewed_at on a report so the client portal can demote it
    below unread reports. Optional client_id filter for ownership safety."""
    if not is_configured() or not report_id:
        return None
    try:
        filt = f'id=eq.{report_id}'
        if client_id:
            filt += f'&client_id=eq.{client_id}'
        r = requests.patch(_rest(f'report_logs?{filt}'),
                           headers=_headers({'Prefer': 'return=representation'}),
                           json={'viewed_at': datetime.now(timezone.utc).isoformat()},
                           timeout=TIMEOUT)
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else None
    except Exception as e:
        print(f'[db] mark_report_viewed failed (non-fatal): {e}')
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


def get_query(query_id):
    """Fetch a single query by id (no embedded client). Used for ownership checks."""
    if not is_configured() or not query_id:
        return None
    try:
        r = requests.get(_rest(f'report_queries?id=eq.{query_id}&select=*'),
                         headers=_headers(), timeout=TIMEOUT)
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else None
    except Exception as e:
        print(f'[db] get_query failed (non-fatal): {e}')
        return None


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


# ---------------- Conversation thread + attachments ----------------
def add_message(query_id, sender_type, sender_email, body, attachments=None):
    """Append a message (text and/or files) to a query thread.
    sender_type: 'client' | 'admin'. Returns the inserted row or None."""
    if not is_configured() or not query_id:
        return None
    data = {
        'query_id': query_id,
        'sender_type': sender_type,
        'sender_email': sender_email,
        'body': body or None,
        'attachments': attachments or [],
    }
    r = requests.post(_rest('query_messages'),
                      headers=_headers({'Prefer': 'return=representation'}),
                      json=data, timeout=TIMEOUT)
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None


def list_messages(query_id):
    """All messages for one query, oldest first (chat order)."""
    if not is_configured() or not query_id:
        return []
    try:
        r = requests.get(_rest(f'query_messages?query_id=eq.{query_id}&select=*&order=created_at.asc'),
                         headers=_headers(), timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f'[db] list_messages failed (non-fatal): {e}')
        return []


def messages_by_query(query_ids):
    """Bulk fetch: {query_id: [messages...]} for the given list of ids."""
    if not is_configured() or not query_ids:
        return {}
    try:
        ids = ','.join(query_ids)
        r = requests.get(_rest(f'query_messages?query_id=in.({ids})&select=*&order=created_at.asc'),
                         headers=_headers(), timeout=TIMEOUT)
        r.raise_for_status()
        out = {}
        for m in r.json():
            out.setdefault(m['query_id'], []).append(m)
        return out
    except Exception as e:
        print(f'[db] messages_by_query failed (non-fatal): {e}')
        return {}


def update_query_status(query_id, status, responded_by=None):
    """Bump a query's status (open/answered/resolved) when a new message lands."""
    if not is_configured() or not query_id:
        return None
    data = {'status': status}
    if responded_by:
        data['responded_by'] = responded_by
        data['responded_at'] = datetime.now(timezone.utc).isoformat()
    try:
        r = requests.patch(_rest(f'report_queries?id=eq.{query_id}'),
                           headers=_headers({'Prefer': 'return=representation'}),
                           json=data, timeout=TIMEOUT)
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else None
    except Exception as e:
        print(f'[db] update_query_status failed (non-fatal): {e}')
        return None


# ---------------- One chat per client (WhatsApp-style) ----------------
def _seed_messages_from_query(q):
    """Synthesise message rows from the legacy report_queries.message /
    response fields, for queries that don't have any query_messages yet."""
    out = []
    if q.get('message') and q['message'] != '(attachment)' and q['message'] != '(chat thread)':
        out.append({
            'id': f"seed-{q['id']}-c",
            'query_id': q['id'],
            'sender_type': 'client',
            'sender_email': None,
            'body': q['message'],
            'attachments': [],
            'created_at': q.get('created_at'),
        })
    if q.get('response'):
        out.append({
            'id': f"seed-{q['id']}-a",
            'query_id': q['id'],
            'sender_type': 'admin',
            'sender_email': q.get('responded_by'),
            'body': q['response'],
            'attachments': [],
            'created_at': q.get('responded_at') or q.get('created_at'),
        })
    return out


def client_messages(client_id):
    """One unified timeline (chronological) of every message between this
    client and the team -- across all of their queries. Includes synthesised
    bubbles for legacy queries that don't have query_messages rows yet."""
    if not is_configured() or not client_id:
        return []
    try:
        r = requests.get(_rest(f'report_queries?client_id=eq.{client_id}&select=*&order=created_at.asc'),
                         headers=_headers(), timeout=TIMEOUT)
        r.raise_for_status()
        queries = r.json()
        if not queries:
            return []
        ids = ','.join(q['id'] for q in queries)
        r2 = requests.get(_rest(f'query_messages?query_id=in.({ids})&select=*&order=created_at.asc'),
                          headers=_headers(), timeout=TIMEOUT)
        r2.raise_for_status()
        by_query = {}
        for m in r2.json():
            by_query.setdefault(m['query_id'], []).append(m)
        timeline = []
        for q in queries:
            if q['id'] in by_query:
                timeline.extend(by_query[q['id']])
            else:
                timeline.extend(_seed_messages_from_query(q))
        timeline.sort(key=lambda m: m.get('created_at') or '')
        return timeline
    except Exception as e:
        print(f'[db] client_messages failed (non-fatal): {e}')
        return []


def get_or_create_thread_query(client_id, subject='Chat'):
    """Find the most-recent query for this client (the chat 'container')
    and reuse it for new messages, or create one if none exists. Returns
    the query_id to attach new messages to."""
    if not is_configured() or not client_id:
        return None
    try:
        r = requests.get(_rest(
            f'report_queries?client_id=eq.{client_id}&select=id&order=created_at.desc&limit=1'),
            headers=_headers(), timeout=TIMEOUT)
        r.raise_for_status()
        rows = r.json()
        if rows:
            return rows[0]['id']
    except Exception as e:
        print(f'[db] thread query lookup failed: {e}')
    try:
        new_q = add_query({
            'client_id': client_id,
            'message': '(chat thread)',
            'subject': subject,
            'status': 'open',
        })
        return new_q['id'] if new_q else None
    except Exception as e:
        print(f'[db] thread query create failed: {e}')
        return None


def list_chats():
    """One row per CLIENT for the PMO dashboard. Returns a list of
    {client, messages, status, last_at, last_query_id} sorted by most
    recent activity first."""
    if not is_configured():
        return []
    try:
        r = requests.get(_rest(
            'report_queries?select=*,client:clients(id,name,email)&order=created_at.asc'),
            headers=_headers(), timeout=TIMEOUT)
        r.raise_for_status()
        queries = r.json()
        if not queries:
            return []
        ids = ','.join(q['id'] for q in queries)
        r2 = requests.get(_rest(f'query_messages?query_id=in.({ids})&select=*&order=created_at.asc'),
                          headers=_headers(), timeout=TIMEOUT)
        r2.raise_for_status()
        by_query = {}
        for m in r2.json():
            by_query.setdefault(m['query_id'], []).append(m)

        by_client = {}
        for q in queries:
            client = q.get('client') or {}
            cid = client.get('id') or q.get('client_id')
            if not cid:
                continue
            chat = by_client.setdefault(cid, {
                'client': client,
                'messages': [],
                'last_query_id': q['id'],
                'has_resolved': False,
            })
            # Track the most-recent query id for this client (queries are
            # iterated oldest->newest so each assignment beats the previous).
            chat['last_query_id'] = q['id']
            if q.get('status') == 'resolved':
                chat['has_resolved'] = True
            if q['id'] in by_query:
                chat['messages'].extend(by_query[q['id']])
            else:
                chat['messages'].extend(_seed_messages_from_query(q))

        chats = []
        for cid, chat in by_client.items():
            chat['messages'].sort(key=lambda m: m.get('created_at') or '')
            chat['last_at'] = chat['messages'][-1].get('created_at') if chat['messages'] else None
            if chat['messages']:
                last = chat['messages'][-1]
                if chat['has_resolved'] and last.get('sender_type') == 'admin':
                    chat['status'] = 'resolved'
                elif last.get('sender_type') == 'admin':
                    chat['status'] = 'answered'
                else:
                    chat['status'] = 'open'
            else:
                chat['status'] = 'open'
            chats.append(chat)
        chats.sort(key=lambda c: c.get('last_at') or '', reverse=True)
        return chats
    except Exception as e:
        print(f'[db] list_chats failed (non-fatal): {e}')
        return []


def count_open_chats():
    """Number of client chats whose latest message is from the client
    (i.e., awaiting a reply)."""
    try:
        return len([c for c in list_chats() if c.get('status') == 'open'])
    except Exception as e:
        print(f'[db] count_open_chats failed (non-fatal): {e}')
        return 0


def _ensure_attachment_bucket():
    """Make sure the Supabase Storage bucket exists and is public.
    Auto-creates it on first use; cached for the rest of the process.
    Returns True if the bucket is ready, False otherwise (logs why)."""
    global _bucket_ready
    if _bucket_ready:
        return True
    url, key = _config()
    if not url or not key:
        return False
    headers = {'Authorization': f'Bearer {key}', 'apikey': key,
               'Content-Type': 'application/json'}
    bucket_url = f"{url}/storage/v1/bucket/{ATTACHMENT_BUCKET}"
    try:
        # Already there?
        r = requests.get(bucket_url, headers=headers, timeout=TIMEOUT)
        if r.status_code == 200:
            info = r.json() or {}
            if not info.get('public'):
                # Flip it public so the URLs we hand out actually work.
                # Supabase update API expects ONLY the fields you want to change.
                pr = requests.put(bucket_url, headers=headers,
                                  json={'public': True}, timeout=TIMEOUT)
                if pr.status_code >= 400:
                    print(f"[db] could not auto-flip bucket to public: "
                          f"{pr.status_code} {pr.text[:300]} -- please toggle "
                          f"'Public bucket' ON in Supabase Dashboard -> Storage -> "
                          f"{ATTACHMENT_BUCKET} -> Edit bucket")
                else:
                    print(f"[db] flipped bucket '{ATTACHMENT_BUCKET}' to public")
            _bucket_ready = True
            return True
        # Not there -> create as public.
        cr = requests.post(f"{url}/storage/v1/bucket", headers=headers,
                           json={'id': ATTACHMENT_BUCKET, 'name': ATTACHMENT_BUCKET,
                                 'public': True}, timeout=TIMEOUT)
        if cr.status_code in (200, 201):
            print(f"[db] auto-created Supabase Storage bucket '{ATTACHMENT_BUCKET}' (public)")
            _bucket_ready = True
            return True
        print(f"[db] bucket create failed: {cr.status_code} {cr.text[:300]}")
        return False
    except Exception as e:
        print(f'[db] _ensure_attachment_bucket exception: {e}')
        return False


def upload_attachment(query_id, filename, content, content_type=None):
    """Upload a file to the Supabase 'query-attachments' bucket and return
    {name, url, size, type}. Returns None if anything fails (logs why).
    The bucket is auto-created (public) on first use, so no manual setup."""
    if not is_configured() or not content:
        return None
    if not _ensure_attachment_bucket():
        print('[db] attachment bucket not available')
        return None
    url, key = _config()
    safe = secure_filename(filename) or 'file'
    path = f"{query_id}/{uuid.uuid4().hex}_{safe}"
    upload_url = f"{url}/storage/v1/object/{ATTACHMENT_BUCKET}/{path}"
    headers = {
        'Authorization': f'Bearer {key}',
        'apikey': key,
        'Content-Type': content_type or 'application/octet-stream',
        'x-upsert': 'true',
    }
    try:
        r = requests.post(upload_url, headers=headers, data=content, timeout=60)
        if r.status_code >= 400:
            print(f"[db] upload failed for {filename!r}: {r.status_code} {r.text[:300]}")
            return None
    except Exception as e:
        print(f'[db] upload_attachment exception: {e}')
        return None
    public_url = f"{url}/storage/v1/object/public/{ATTACHMENT_BUCKET}/{path}"
    return {
        'name': filename,
        'url': public_url,
        'size': len(content),
        'type': content_type or '',
    }


def upload_report_file(client_id, filename, content, content_type=None):
    """Upload a report file (PDF/PPT/etc) so the client can open it from
    the portal. Stored under reports/<client_id>/... in the same bucket."""
    return upload_attachment(f'reports/{client_id}', filename, content, content_type)


# ---------------- Service credentials (e.g. agency refresh_token) ----
def save_service_credential(key, value):
    """Upsert a service credential. Safe on missing config / errors."""
    if not is_configured() or not key or value is None:
        return None
    try:
        # PostgREST upsert via the 'resolution=merge-duplicates' Prefer header,
        # which uses the primary key (key) to decide insert vs update.
        r = requests.post(
            _rest('service_credentials'),
            headers=_headers({
                'Prefer': 'resolution=merge-duplicates,return=representation',
            }),
            json={'key': key, 'value': value,
                  'updated_at': datetime.now(timezone.utc).isoformat()},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        print(f'[db] save_service_credential failed (non-fatal): {e}')
        return None


def get_service_credential(key):
    """Look up a stored service credential. Returns the value string or None."""
    if not is_configured() or not key:
        return None
    try:
        r = requests.get(
            _rest(f'service_credentials?key=eq.{key}&select=value'),
            headers=_headers(), timeout=TIMEOUT,
        )
        r.raise_for_status()
        rows = r.json()
        return rows[0]['value'] if rows else None
    except Exception as e:
        print(f'[db] get_service_credential failed (non-fatal): {e}')
        return None
