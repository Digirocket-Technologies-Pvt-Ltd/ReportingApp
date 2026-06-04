from flask import Flask, Response, render_template, redirect, url_for, request, jsonify, session, flash, send_file, g
from auth import is_authenticated, refresh_token_if_needed, logout_user, get_user_info, get_session_info, is_pmo_admin
import db
from ga4 import get_ga4_properties, get_ga4_data, get_property_name, get_ga4_overview, get_ga4_acquisition, get_ga4_extra, get_ga4_daily_overview
from gsc import get_gsc_sites, get_gsc_detailed_data, normalize_gsc_property, get_gsc_summary
from gmb import get_gmb_data, gmb_debug
from gmc import get_gmc_data
from data_processing import validate_dates
from google.oauth2.credentials import Credentials
from config import CLIENT_ID, CLIENT_SECRET, REDIRECT_URI, SCOPES
import os
import time
import base64
from PIL import Image
import io
from datetime import datetime, timedelta, timezone
import requests
from pdf_processing import build_slide_images, build_pdf_from_images, build_editable_pptx, build_pptx_from_images, build_editable_image_pptx
from werkzeug.utils import secure_filename
import shutil
from image_explanation import explain_image_with_gemini
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import time as _time

app = Flask(__name__, static_folder='static')


def init_routes(app):
    # =====================================================================
    #  AGENCY GOOGLE TOKEN (used to fetch GA4/GSC for clients)
    # =====================================================================
    # When a PMO admin logs in we save their OAuth refresh_token to
    # service_credentials. Client dashboards then mint a fresh access_token
    # from it on demand (cached in-process for the token's lifetime ~1h).
    # This lets clients see THEIR property's data even though their own
    # Google account doesn't have direct access to it.
    _agency_cache = {'token': None, 'expires_at': 0.0}

    # ---------- /view_combined_data response cache ------------------------
    # Same user + same property + same date range + same flags within 5 min
    # = serve from memory (skip ~6+ Google API round-trips, ~1 Kimi call).
    # Page refresh and back-button feels instant after the first load.
    # Stored value is the assembled context dict (everything passed to
    # render_template); we re-render at request time so the active session
    # info / flash messages still reflect the current request.
    _CD_CACHE = {}
    _CD_TTL = 300.0  # seconds

    def _cd_cache_key(args, user_email):
        """Stable cache key from request.args + the requesting user. Sorting
        ensures argument order in the URL doesn't break the cache."""
        h = hashlib.sha1()
        h.update((user_email or '').encode('utf-8'))
        # request.args is a MultiDict - flatten consistently
        for k in sorted(args.keys()):
            for v in sorted(args.getlist(k)):
                h.update(b'\x00'); h.update(k.encode('utf-8'))
                h.update(b'\x01'); h.update(str(v).encode('utf-8'))
        return h.hexdigest()

    def _agency_access_token():
        rt = db.get_service_credential('agency_refresh_token')
        if not rt:
            return None
        now = datetime.now().timestamp()
        if _agency_cache['token'] and _agency_cache['expires_at'] > now + 60:
            return _agency_cache['token']
        try:
            resp = requests.post('https://oauth2.googleapis.com/token', data={
                'client_id': CLIENT_ID,
                'client_secret': CLIENT_SECRET,
                'refresh_token': rt,
                'grant_type': 'refresh_token',
            }, timeout=20)
            if resp.status_code != 200:
                print(f'[agency] token refresh failed: {resp.status_code} {resp.text[:200]}')
                return None
            data = resp.json()
            _agency_cache['token'] = data['access_token']
            _agency_cache['expires_at'] = now + data.get('expires_in', 3600)
            return _agency_cache['token']
        except Exception as e:
            print(f'[agency] token refresh exception: {e}')
            return None

    @app.before_request
    def _maybe_use_agency_token():
        """For client (non-admin) requests, swap session's access_token to the
        agency's token for the duration of this request, so Google API calls
        succeed even though the client's own Google account has no access.
        Restored in after_request so we don't persist the swap.

        For admins, opportunistically save their refresh_token as the agency
        credential (idempotent, once per session) so they don't have to
        log out + log back in after deploying this feature."""
        # One-time backfill: existing sessions logged in before this code
        # shipped won't have 'is_client'. Compute and cache it once.
        if 'is_client' not in session and is_authenticated():
            try:
                if is_pmo_admin():
                    session['is_client'] = False
                elif db.is_configured() and db.get_client_by_email(session.get('user_email')):
                    session['is_client'] = True
                else:
                    session['is_client'] = False
            except Exception:
                session['is_client'] = False

        # Admin path: silently mirror their refresh_token into the
        # service_credentials table so client dashboards can use it.
        # ALWAYS overwrites (latest admin login wins -> no stale tokens).
        # The session flag is only set on a successful save, so an admin
        # whose first request fails will retry on the next one.
        if (is_authenticated() and is_pmo_admin()
                and not session.get('_agency_creds_synced')):
            rt = session.get('refresh_token')
            if rt and db.is_configured():
                try:
                    ok = db.save_service_credential('agency_refresh_token', rt)
                    if ok:
                        print(f"[agency] saved refresh_token from "
                              f"{session.get('user_email')}'s session "
                              f"(len={len(rt)})")
                        session['_agency_creds_synced'] = True
                    else:
                        print('[agency] save returned None - will retry next request')
                except Exception as e:
                    print(f'[agency] opportunistic save failed: {e}')
            elif not rt:
                # No refresh_token in this admin session -> they need a fresh
                # login (Google only hands out refresh_tokens with prompt=consent,
                # which our /login route already requests).
                print(f"[agency] admin {session.get('user_email')} has no "
                      f"refresh_token in session - ask them to log out + log in")
                session['_agency_creds_synced'] = True   # don't retry; can't fix automatically

        if not session.get('is_client'):
            return
        agency_token = _agency_access_token()
        if not agency_token:
            return  # no agency creds yet; let downstream code surface the error
        g._original_access_token = session.get('access_token')
        g._original_refresh_token = session.get('refresh_token')
        session['access_token'] = agency_token
        rt = db.get_service_credential('agency_refresh_token')
        if rt:
            session['refresh_token'] = rt
        g._token_swapped = True

    @app.after_request
    def _restore_user_token(response):
        """Undo the agency-token swap so the user's own tokens are what gets
        persisted back to the session cookie."""
        if getattr(g, '_token_swapped', False):
            orig_at = getattr(g, '_original_access_token', None)
            orig_rt = getattr(g, '_original_refresh_token', None)
            if orig_at is not None:
                session['access_token'] = orig_at
            elif 'access_token' in session:
                session.pop('access_token', None)
            if orig_rt is not None:
                session['refresh_token'] = orig_rt
            elif 'refresh_token' in session:
                session.pop('refresh_token', None)
        return response

    @app.after_request
    def _gzip_response(response):
        """Gzip-compress text responses (HTML / JSON / CSS / JS / SVG) on the
        fly. The big combined-data page is ~160 KB raw and shrinks to ~20-30 KB
        gzipped, which is the single biggest win for perceived page-load speed
        on slower connections. Skip if:
            * client didn't ask for gzip
            * payload is small (< 1 KB - not worth the CPU)
            * Content-Type is already compressed (images, fonts, PDFs, video)
            * response was already encoded (don't double-encode)
        """
        try:
            if response.direct_passthrough:
                return response
            accept = (request.headers.get('Accept-Encoding') or '').lower()
            if 'gzip' not in accept:
                return response
            if response.status_code < 200 or response.status_code >= 300:
                return response
            if response.headers.get('Content-Encoding'):
                return response
            ctype = (response.content_type or '').split(';', 1)[0].strip().lower()
            compressible = (
                ctype.startswith('text/')
                or ctype in ('application/json', 'application/javascript',
                             'application/xml', 'application/xhtml+xml',
                             'image/svg+xml')
            )
            if not compressible:
                return response
            data = response.get_data()
            if len(data) < 1024:
                return response
            buf = io.BytesIO()
            with gzip.GzipFile(fileobj=buf, mode='wb', compresslevel=6, mtime=0) as gz:
                gz.write(data)
            response.set_data(buf.getvalue())
            response.headers['Content-Encoding'] = 'gzip'
            response.headers['Content-Length'] = str(len(response.get_data()))
            # Tell shared caches the encoding depends on this request header.
            vary = response.headers.get('Vary')
            if vary:
                if 'accept-encoding' not in vary.lower():
                    response.headers['Vary'] = vary + ', Accept-Encoding'
            else:
                response.headers['Vary'] = 'Accept-Encoding'
        except Exception as e:
            print(f'[gzip] skipping compression: {e}')
        return response

    @app.after_request
    def add_no_cache_headers(response):
        """Keep dynamic HTML / JSON uncached (fresh data every load) but let
        static assets (CSS / JS / fonts / images / SVG) cache aggressively in
        the browser. Without this, every page navigation re-downloaded the
        full Tailwind + font-awesome + Chart.js + html2canvas bundle and the
        brand CSS / JS, which made navigation and refreshes feel sluggish.

        The generated PDF report stays uncached so a freshly-built report
        replaces the old one in the /report iframe instead of serving stale."""
        # Only cache GET responses that succeeded -> never cache an error
        # page just because it happens to be a CSS request.
        if request.method == 'GET' and 200 <= response.status_code < 300:
            path = (request.path or '').lower()
            # Cacheable static assets served from /static/* (1 day).
            # Skip the generated report PDF so report regenerations are visible.
            if (path.startswith('/static/')
                    and not path.endswith('/analytics_report.pdf')
                    and path.endswith((
                        '.css', '.js', '.mjs', '.svg', '.png', '.jpg', '.jpeg',
                        '.gif', '.webp', '.ico', '.woff', '.woff2', '.ttf',
                        '.otf', '.eot', '.map',
                    ))):
                response.headers['Cache-Control'] = 'public, max-age=86400'
                return response
            # Favicon endpoints (served by Flask, not /static/) — cache a day.
            if path in ('/favicon.ico', '/favicon.svg'):
                response.headers['Cache-Control'] = 'public, max-age=86400'
                return response
        # Everything else (HTML pages, JSON API, generated PDF) -> no cache.
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response

    @app.route('/')
    def index():
        if is_authenticated():
            return redirect(url_for('dashboard'))
        return render_template('index.html')

    # Favicon: DigiRocket brand green + chart-line glyph.
    # Chrome quietly REJECTS SVG-content served at /favicon.ico (it expects
    # ICO/PNG bytes there), so we serve a real PNG at /favicon.ico (rendered
    # once via Pillow and cached) and the same artwork as SVG at /favicon.svg
    # for browsers that prefer the vector version.
    _FAVICON_SVG = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        '<rect width="64" height="64" rx="14" fill="#C9F31D"/>'
        '<path d="M12 44 L24 30 L34 38 L52 18" fill="none" '
        'stroke="#0a0d0e" stroke-width="6" stroke-linecap="round" stroke-linejoin="round"/>'
        '<circle cx="52" cy="18" r="4" fill="#0a0d0e"/>'
        '</svg>'
    )
    _favicon_png_cache = {'bytes': None}

    def _build_favicon_png():
        from PIL import Image, ImageDraw
        import io
        img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([(0, 0), (63, 63)], radius=14, fill=(201, 243, 29, 255))
        line = (10, 13, 14, 255)
        pts = [(12, 44), (24, 30), (34, 38), (52, 18)]
        for i in range(len(pts) - 1):
            d.line([pts[i], pts[i + 1]], fill=line, width=6, joint='curve')
        d.ellipse([(48, 14), (56, 22)], fill=line)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return buf.getvalue()

    @app.route('/favicon.ico')
    def favicon_ico():
        if _favicon_png_cache['bytes'] is None:
            _favicon_png_cache['bytes'] = _build_favicon_png()
        return Response(_favicon_png_cache['bytes'], mimetype='image/png',
                        headers={'Cache-Control': 'public, max-age=86400'})

    @app.route('/favicon.svg')
    def favicon_svg():
        return Response(_FAVICON_SVG, mimetype='image/svg+xml',
                        headers={'Cache-Control': 'public, max-age=86400'})

    @app.route('/debug-gmb')
    def debug_gmb():
        """Browser-visitable diagnostic: shows exactly where the GMB API chain breaks.
        Open /debug-gmb after logging in and share the JSON output."""
        if not is_authenticated():
            return redirect(url_for('login'))
        refresh_token_if_needed()
        token = session.get('access_token')
        # also report which scopes the token actually has
        scope_info = {}
        try:
            ti = requests.get('https://www.googleapis.com/oauth2/v3/tokeninfo',
                              params={'access_token': token}, timeout=30).json()
            scope_info = {'granted_scopes': ti.get('scope', ''), 'email': ti.get('email')}
        except Exception as e:
            scope_info = {'error': str(e)}
        try:
            start = datetime.now() - timedelta(days=30)
            end = datetime.now() - timedelta(days=3)
            result = gmb_debug(token, start, end)
        except Exception as e:
            result = {'fatal': str(e)}
        return jsonify({'token_scopes': scope_info, 'gmb_diagnostic': result})

    @app.route('/login')
    def login():
        session.clear()
        from urllib.parse import urlencode
        params = {
            'client_id': CLIENT_ID,
            'redirect_uri': REDIRECT_URI,
            'response_type': 'code',
            'scope': ' '.join(SCOPES),
            'access_type': 'offline',
            # select_account = show the Google account chooser (so the right account can be picked)
            # consent = ask again for new permissions
            'prompt': 'select_account consent',
        }
        auth_url = 'https://accounts.google.com/o/oauth2/auth?' + urlencode(params)
        return redirect(auth_url)

    @app.route('/logout')
    def logout():
        """Logout route to clear session and redirect to home"""
        if logout_user():
            flash('You have been successfully logged out.', 'success')
        else:
            flash('There was an issue logging you out.', 'error')
        return redirect(url_for('index'))

    @app.route('/oauth2callback')
    def oauth2callback():
        code = request.args.get('code')
        if not code:
            flash('Authorization failed', 'error')
            return redirect(url_for('index'))

        try:
            token_url = 'https://oauth2.googleapis.com/token'
            data = {
                'code': code,
                'client_id': CLIENT_ID,
                'client_secret': CLIENT_SECRET,
                'redirect_uri': REDIRECT_URI,
                'grant_type': 'authorization_code'
            }
            response = requests.post(token_url, data=data)
            response.raise_for_status()

            token_data = response.json()
            session['access_token'] = token_data['access_token']
            session['refresh_token'] = token_data.get('refresh_token')
            session['token_expiry'] = datetime.now().timestamp() + token_data['expires_in']
            session['login_time'] = datetime.now().timestamp()

            # Get user information
            get_user_info()

            # Cache the user's role on the session so before_request can decide
            # whether to swap to agency credentials without a DB hit per request.
            session['is_client'] = False
            try:
                if is_pmo_admin():
                    # Save this admin's refresh_token as the "agency" credentials
                    # so client dashboards can fetch GA4/GSC data on the
                    # client's behalf (clients usually don't have direct access).
                    rt = session.get('refresh_token')
                    if rt and db.is_configured():
                        db.save_service_credential('agency_refresh_token', rt)
                elif db.is_configured() \
                        and db.get_client_by_email(session.get('user_email')):
                    session['is_client'] = True
            except Exception as e:
                print(f'[login] role/agency setup failed (non-fatal): {e}')

            flash('Successfully logged in!', 'success')
            # Recognised clients land on their portal; team/admins go to dashboard.
            if session.get('is_client'):
                return redirect(url_for('client_portal'))
            return redirect(url_for('dashboard'))
        except requests.exceptions.RequestException as e:
            flash(f'Login error: {str(e)}', 'error')
            return redirect(url_for('index'))

    @app.route('/dashboard')
    def dashboard():
        if not is_authenticated():
            flash('Please log in to access the dashboard.', 'warning')
            return redirect(url_for('login'))

        # Clients must use their own filtered portal dashboard so they never
        # see other clients' properties in the dropdowns.
        if session.get('is_client'):
            return redirect(url_for('client_dashboard'))

        if not refresh_token_if_needed():
            flash('Session expired. Please log in again.', 'warning')
            return redirect(url_for('login'))

        try:
            # NOTE: do NOT push these into ThreadPoolExecutor. Both fetchers
            # touch Flask's `session` proxy, which is request-context-local
            # and unavailable in worker threads - calling them off-thread
            # raised "Working outside of request context", which the except
            # below turned into a redirect to /, and / redirected straight
            # back to /dashboard -> infinite loop. The 5-minute cache added
            # in ga4.py / gsc.py already makes the SECOND+ loads instant,
            # which is the case that mattered.
            ga4_properties, error = get_ga4_properties(session)
            gsc_sites = get_gsc_sites(session)

            # Get session info for display
            session_info = get_session_info()

            return render_template(
                'dashboard.html',
                ga4_properties=ga4_properties,
                gsc_sites=gsc_sites,
                ga4_error=error if error else None,
                session_info=session_info,
                is_admin=is_pmo_admin()
            )
        except Exception as e:
            # IMPORTANT: do NOT redirect to url_for('index') here. `/` checks
            # is_authenticated() and bounces straight back to /dashboard -
            # so if THIS handler ever errors twice in a row, Chrome would
            # see /dashboard -> / -> /dashboard -> / ... and abort with
            # ERR_TOO_MANY_REDIRECTS. Rendering an inline error page breaks
            # any redirect chain and shows the user what's actually wrong.
            print(f'[dashboard] error: {e}')
            return (
                "<html><head><title>Dashboard error</title></head><body "
                "style='font-family:sans-serif;max-width:680px;margin:60px auto;"
                "padding:0 20px;color:#1f2937'>"
                f"<h1 style='color:#dc2626'>Could not load the dashboard</h1>"
                f"<p>{str(e)}</p>"
                "<p><a href='/logout' style='display:inline-block;background:"
                "#4f46e5;color:#fff;padding:10px 18px;border-radius:8px;"
                "text-decoration:none'>Log out and try again</a></p>"
                "</body></html>",
                500,
            )

    @app.route('/find-ga4-data')
    def find_ga4_data():
        """Scan ALL GA4 properties the user can access and report which ones
        actually have data (last 90 days). Saves trial-and-error."""
        if not is_authenticated() or not refresh_token_if_needed():
            return redirect(url_for('login'))

        from datetime import datetime as _dt, timedelta as _td
        token = session['access_token']
        headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

        # 1) Get all properties
        try:
            summaries = requests.get(
                'https://analyticsadmin.googleapis.com/v1beta/accountSummaries',
                headers=headers).json()
        except Exception as e:
            return f"<pre>Error getting properties: {e}</pre>"

        props = []
        for account in summaries.get('accountSummaries', []):
            for p in account.get('propertySummaries', []):
                props.append({
                    'id': p['property'].split('/')[-1],
                    'name': p.get('displayName', 'Unnamed'),
                })

        # 2) Check each property for data in the last 90 days
        end = _dt.now().strftime('%Y-%m-%d')
        start = (_dt.now() - _td(days=90)).strftime('%Y-%m-%d')
        with_data, empty = [], []
        for prop in props:
            url = f"https://analyticsdata.googleapis.com/v1beta/properties/{prop['id']}:runReport"
            body = {
                "metrics": [{"name": "activeUsers"}, {"name": "screenPageViews"}],
                "dateRanges": [{"startDate": start, "endDate": end}],
            }
            try:
                r = requests.post(url, headers=headers, json=body, timeout=20)
                if r.status_code == 200:
                    rows = r.json().get('rows', [])
                    if rows:
                        users = int(rows[0]['metricValues'][0]['value'])
                        views = int(rows[0]['metricValues'][1]['value'])
                        if users > 0 or views > 0:
                            with_data.append({**prop, 'users': users, 'views': views})
                        else:
                            empty.append(prop)
                    else:
                        empty.append(prop)
            except Exception:
                pass

        with_data.sort(key=lambda x: x['users'], reverse=True)

        # 3) Build a simple HTML report
        html = """
        <html><head><title>GA4 Properties With Data</title>
        <style>
            body { font-family: Arial, sans-serif; max-width: 800px; margin: 40px auto; padding: 0 20px; }
            h1 { color: #4f46e5; }
            .ok { background:#ecfdf5; border:1px solid #10b981; border-radius:10px; padding:14px 18px; margin:10px 0; }
            .ok b { font-size:16px; color:#065f46; }
            .id { color:#6b7280; font-size:13px; }
            .stats { color:#047857; font-weight:600; }
            .empty { color:#9ca3af; }
            a.btn { display:inline-block; background:#4f46e5; color:#fff; padding:10px 18px;
                    border-radius:8px; text-decoration:none; margin-top:20px; }
        </style></head><body>
        """
        html += f"<h1>✅ {len(with_data)} properties have data (last 90 days)</h1>"
        html += "<p>Select these in the dashboard's GA4 dropdown (identify by Property ID):</p>"
        for p in with_data:
            html += (f"<div class='ok'><b>{p['name']}</b><br>"
                     f"<span class='id'>Property ID: {p['id']}</span><br>"
                     f"<span class='stats'>{p['users']:,} users &nbsp;|&nbsp; {p['views']:,} page views</span></div>")
        if not with_data:
            html += "<p>⚠️ No data found in any property for the last 90 days.</p>"
        html += f"<p class='empty'>({len(empty)} properties are empty - no tracking data coming in)</p>"
        html += "<a class='btn' href='/dashboard'>← Back to Dashboard</a>"
        html += "</body></html>"
        return html

    @app.route('/view_combined_data')
    def view_combined_data():
        if not is_authenticated():
            flash('Please log in to access analytics data.', 'warning')
            return redirect(url_for('login'))

        if not refresh_token_if_needed():
            flash('Session expired. Please log in again.', 'warning')
            return redirect(url_for('login'))

        # Fast-path: identical request from this user within the TTL window
        # serves the cached render context without re-running the 10+
        # API calls or the AI summary. Add ?refresh=1 to bypass.
        _cd_key = _cd_cache_key(request.args, session.get('user_email'))
        _bypass = (request.args.get('refresh') or '').lower() in ('1', 'true', 'yes', 'on')
        if not _bypass:
            _hit = _CD_CACHE.get(_cd_key)
            if _hit and _hit[0] > _time.time():
                _ctx = dict(_hit[1])
                # session_info / report_exists may change between hits - refresh
                _ctx['session_info'] = get_session_info()
                # Re-check role too: cheap (env-var lookup) and protects against
                # stale entries from before this field was cached.
                _ctx['is_admin'] = is_pmo_admin()
                _report_pdf_path = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    'static', 'images', 'analytics_report.pdf')
                _ctx['report_exists'] = os.path.exists(_report_pdf_path)
                _ctx['report_pdf_url'] = (url_for('static', filename='images/analytics_report.pdf')
                                          if _ctx['report_exists'] else None)
                # Re-prime report_ctx for the editable PPTX route
                session['report_ctx'] = {
                    'ga4_property_id': request.args.get('ga4_property'),
                    'ga4_property_name': _ctx.get('ga4_property_name'),
                    'gsc_site': request.args.get('gsc_site'),
                    'start': _ctx.get('start_date'),
                    'end': _ctx.get('end_date'),
                    'metrics': request.args.getlist('metrics') or [
                        'new_users', 'active_users', 'returning_users', 'sessions'],
                    'compare': _ctx.get('compare'),
                }
                return render_template('combined_data.html', **_ctx)

        try:
            ga4_property_id = request.args.get('ga4_property')
            gsc_site_url = request.args.get('gsc_site')
            start_date_str = request.args.get('start_date')
            end_date_str = request.args.get('end_date')
            selected_metrics = request.args.getlist('metrics') or [
                'new_users', 'active_users', 'returning_users', 'sessions']
            compare = (request.args.get('compare') or '').lower() in ('1', 'true', 'on', 'yes')

            # PRIVACY: a logged-in client can only ever see data for the GA4
            # property and GSC site that the team has linked to them. Override
            # whatever was in the URL so a crafted link can't leak another
            # client's data.
            if session.get('is_client'):
                client = _current_client()
                if not client:
                    flash('Your account is not linked to a client profile.', 'warning')
                    return redirect(url_for('client_portal'))
                allowed_ga4_set = {x.strip() for x in (client.get('ga4_property_id') or '').split(',') if x.strip()}
                allowed_gsc_set = {normalize_gsc_property(x.strip())
                                   for x in (client.get('gsc_property_id') or '').split(',') if x.strip()}
                if allowed_ga4_set:
                    req_ga4 = (ga4_property_id or '').strip()
                    if req_ga4 not in allowed_ga4_set:
                        ga4_property_id = sorted(allowed_ga4_set)[0]
                else:
                    flash('No GA4 property is linked to your account. Please contact your account manager.', 'warning')
                    return redirect(url_for('client_dashboard'))
                if allowed_gsc_set:
                    req_gsc = (gsc_site_url or '').strip()
                    if req_gsc not in allowed_gsc_set:
                        gsc_site_url = sorted(allowed_gsc_set)[0]
                elif not gsc_site_url:
                    flash('No Search Console site is linked to your account.', 'warning')
                    return redirect(url_for('client_dashboard'))

            if not all([ga4_property_id, gsc_site_url, start_date_str, end_date_str]):
                flash('Missing required parameters.', 'error')
                return redirect(url_for('dashboard'))

            start_date, end_date = validate_dates(start_date_str, end_date_str)

            credentials = Credentials(
                token=session['access_token'],
                refresh_token=session['refresh_token'],
                token_uri='https://oauth2.googleapis.com/token',
                client_id=CLIENT_ID,
                client_secret=CLIENT_SECRET,
                scopes=SCOPES
            )

            # Compute previous-period dates up-front so the parallel fan-out
            # below can include them in the same wave (no second round-trip).
            compare_flag = compare
            prev_start = prev_end = None
            if compare_flag:
                length = (end_date - start_date).days + 1
                prev_end = start_date - timedelta(days=1)
                prev_start = prev_end - timedelta(days=length - 1)

            want_gmb = (request.args.get('gmb') or '').lower() in ('1', 'true', 'on', 'yes')
            want_gmc = (request.args.get('gmc') or '').lower() in ('1', 'true', 'on', 'yes')

            access_token = session['access_token']

            # All of these are independent network calls (GA4, GSC, GMB, GMC,
            # property-name lookup). Running them in parallel shrinks the page
            # wait from "sum of every API" to "slowest single API".
            def _ga4_main():
                return get_ga4_data(access_token, ga4_property_id, start_date, end_date) if ga4_property_id else None
            def _gsc_main():
                return get_gsc_detailed_data(credentials, gsc_site_url, start_date, end_date) if gsc_site_url else None
            def _ga4_name():
                return get_property_name(access_token, ga4_property_id) if ga4_property_id else (ga4_property_id or '')
            def _ga4_acq():
                if not ga4_property_id:
                    return []
                return get_ga4_acquisition(access_token, ga4_property_id, start_date, end_date, prev_start, prev_end)
            def _ga4_x():
                return get_ga4_extra(access_token, ga4_property_id, start_date, end_date) if ga4_property_id else {}
            def _ga4_d():
                return get_ga4_daily_overview(access_token, ga4_property_id, start_date, end_date) if ga4_property_id else {}
            def _prev_ov():
                if not (compare_flag and ga4_property_id):
                    return {}
                return get_ga4_overview(access_token, ga4_property_id, prev_start, prev_end)
            def _prev_gsc():
                if not (compare_flag and gsc_site_url):
                    return {}
                return get_gsc_summary(credentials, gsc_site_url, prev_start, prev_end)
            def _gmb():
                if not want_gmb:
                    return {}
                try:
                    return get_gmb_data(access_token, start_date, end_date)
                except Exception as e:
                    print(f"GMB fetch error: {e}")
                    return {}
            def _gmc():
                if not want_gmc:
                    return {}
                try:
                    return get_gmc_data(access_token, gsc_site_url, start_date, end_date)
                except Exception as e:
                    print(f"GMC fetch error: {e}")
                    return {}

            with ThreadPoolExecutor(max_workers=10) as _ex:
                _f_ga4 = _ex.submit(_ga4_main)
                _f_gsc = _ex.submit(_gsc_main)
                _f_name = _ex.submit(_ga4_name)
                _f_acq = _ex.submit(_ga4_acq)
                _f_extra = _ex.submit(_ga4_x)
                _f_daily = _ex.submit(_ga4_d)
                _f_prev_ov = _ex.submit(_prev_ov)
                _f_prev_gsc = _ex.submit(_prev_gsc)
                _f_gmb = _ex.submit(_gmb)
                _f_gmc = _ex.submit(_gmc)

                ga4_data = _f_ga4.result()
                gsc_data = _f_gsc.result()
                ga4_property_name = _f_name.result()
                acquisition = _f_acq.result() or []
                ga4_extra = _f_extra.result() or {}
                ga4_daily = _f_daily.result() or {}
                _prev_ov_result = _f_prev_ov.result() or {}
                _prev_gsc_result = _f_prev_gsc.result() or {}
                gmb_data = _f_gmb.result() or {}
                gmc_data = _f_gmc.result() or {}

            if ga4_data is None and gsc_data is None:
                if session.get('is_client'):
                    flash('Could not fetch your live data. Most likely the agency '
                          'credentials are not saved yet (an admin needs to log in '
                          'once) or your GA4 property ID is wrong. The team has '
                          'been notified in the logs.', 'error')
                    return redirect(url_for('client_dashboard'))
                flash('Failed to fetch data from both GA4 and Search Console.', 'error')
                return redirect(url_for('dashboard'))

            # Remember what this report is for, so the editable PPTX can be built
            # on demand later (re-fetches the data) from /download-report-pptx.
            session['report_ctx'] = {
                'ga4_property_id': ga4_property_id,
                'ga4_property_name': ga4_property_name,
                'gsc_site': gsc_site_url,
                'start': start_date.strftime('%Y-%m-%d'),
                'end': end_date.strftime('%Y-%m-%d'),
                'metrics': selected_metrics,
                'compare': compare,
            }

            # Get session info for display
            session_info = get_session_info()

            # Split selected overview metrics into infographics of max 4 each
            metric_groups = [selected_metrics[i:i + 4] for i in range(0, len(selected_metrics), 4)]

            # ---- Comparison with the immediately-previous period (optional) ----
            # The prev_start / prev_end dates and the prev-period API results
            # were all fetched in the parallel wave above; here we only do the
            # arithmetic to produce the deltas the template renders.
            ov_change, gsc_change, prev_label = {}, {}, ''

            def _pct(cur, prev):
                try:
                    cur = float(cur); prev = float(prev)
                except (TypeError, ValueError):
                    return None
                if prev == 0:
                    return None
                return round((cur - prev) / prev * 100, 1)

            if compare and prev_start and prev_end:
                prev_label = f"{prev_start.strftime('%Y-%m-%d')} – {prev_end.strftime('%Y-%m-%d')}"
                cur_ov = (ga4_data or {}).get('overview') or {}
                prev_ov = _prev_ov_result
                for k in ['new_users', 'active_users', 'returning_users', 'sessions',
                          'bounce_rate', 'views', 'event_count']:
                    ov_change[k] = _pct(cur_ov.get(k), prev_ov.get(k))
                ov_change['avg_engagement_per_session'] = _pct(cur_ov.get('avg_engagement_seconds'),
                                                               prev_ov.get('avg_engagement_seconds'))
                if gsc_data and gsc_data.get('summary'):
                    cur_s = gsc_data['summary']
                    prev_s = _prev_gsc_result
                    gsc_change = {
                        'clicks': _pct(cur_s.get('total_clicks'), prev_s.get('clicks')),
                        'impressions': _pct(cur_s.get('total_impressions'), prev_s.get('impressions')),
                        'ctr': _pct(str(cur_s.get('average_ctr', '0')).replace('%', ''), prev_s.get('ctr')),
                        'position': _pct(prev_s.get('position'), str(cur_s.get('average_position', '0'))),
                    }

            # Manual indexed pages count (user types it from Search Console)
            indexed_pages = (request.args.get('indexed_pages') or '').strip()

            # AI Insights & Action Plan (optional, Kimi) - only when requested.
            # We feed the AI EVERY section of the report (overview totals +
            # period change, top pages, top events, acquisition channels,
            # countries, devices, landing pages, Search Console summary +
            # top queries / pages / countries / devices, GMB, GMC) so the
            # strategy memo is grounded in the same numbers the client sees.
            ai_text = ''
            if (request.args.get('ai_insights') or '').lower() in ('1', 'true', 'on', 'yes'):
                try:
                    from ai_vision import ai_summary
                    ov = (ga4_data or {}).get('overview') or {}
                    gs = (gsc_data or {}).get('summary') or {}

                    period_label = f"{start_date.strftime('%b %d, %Y')} – {end_date.strftime('%b %d, %Y')}"
                    period_days = (end_date - start_date).days + 1

                    def _fmt_pct(v):
                        if v is None:
                            return ''
                        sign = '+' if v >= 0 else ''
                        return f" ({sign}{v}% vs prev)"

                    lines = []
                    lines.append(f"PROPERTY: {ga4_property_name}")
                    lines.append(f"WEBSITE: {gsc_site_url}")
                    lines.append(f"PERIOD: {period_label} ({period_days} days)")
                    if compare and prev_label:
                        lines.append(f"PREVIOUS PERIOD (for comparison): {prev_label}")
                    if indexed_pages:
                        lines.append(f"INDEXED PAGES (manual, from Search Console): {indexed_pages}")

                    # GA4 overview block with period-over-period deltas
                    lines.append("\n--- GA4 OVERVIEW ---")
                    for k, label in [
                        ('active_users', 'Active Users'),
                        ('new_users', 'New Users'),
                        ('returning_users', 'Returning Users'),
                        ('sessions', 'Sessions'),
                        ('views', 'Page Views'),
                        ('event_count', 'Events'),
                        ('bounce_rate', 'Bounce Rate (%)'),
                        ('avg_engagement_per_session', 'Avg Engagement / Session'),
                    ]:
                        v = ov.get(k, 'n/a')
                        chg = ov_change.get(k) if compare else None
                        lines.append(f"  {label}: {v}{_fmt_pct(chg)}")

                    # Top pages
                    pm = (ga4_data or {}).get('pageMetrics') or []
                    if pm:
                        lines.append("\n--- TOP 10 PAGES (GA4, by views) ---")
                        for p in pm[:10]:
                            lines.append(f"  {p['page']} — views={p['views']}, users={p['users']}, avg_dur={p['avgDuration']}")

                    # Top events
                    em = (ga4_data or {}).get('eventMetrics') or []
                    if em:
                        lines.append("\n--- TOP 10 EVENTS (GA4) ---")
                        for e in em[:10]:
                            lines.append(f"  {e['event']} — count={e['count']}, per_user={e['perUser']:.2f}")

                    # Acquisition channels with deltas
                    if acquisition:
                        lines.append("\n--- ACQUISITION BY CHANNEL (top 10) ---")
                        for ch in acquisition[:10]:
                            cur = ch.get('current') or {}
                            chg = ch.get('change') or {}
                            tu_chg = _fmt_pct(chg.get('total_users'))
                            lines.append(
                                f"  {ch.get('channel')} — users={cur.get('total_users', 0)}{tu_chg}, "
                                f"new={cur.get('new_users', 0)}, sessions={cur.get('sessions', 0)}, "
                                f"events={cur.get('event_count', 0)}"
                            )

                    # Geo (GA4)
                    cm = (ga4_data or {}).get('countryMetrics') or []
                    if cm:
                        lines.append("\n--- TOP 10 COUNTRIES (GA4) ---")
                        for c in cm[:10]:
                            lines.append(
                                f"  {c['country']} — users={c['users']}, new={c['newUsers']}, "
                                f"engaged_sessions={c['engagedSessions']}, engagement_rate={c['engagementRate']}"
                            )

                    # Tech / Landing pages
                    if ga4_extra:
                        tech = ga4_extra.get('tech') or []
                        if tech:
                            lines.append("\n--- DEVICE BREAKDOWN (GA4) ---")
                            for d in tech:
                                lines.append(f"  {d['device']} — users={d['users']}, sessions={d['sessions']}, views={d['views']}")
                        landing = ga4_extra.get('landing') or []
                        if landing:
                            lines.append("\n--- TOP 10 LANDING PAGES (GA4) ---")
                            for lp in landing[:10]:
                                lines.append(f"  {lp['page']} — sessions={lp['sessions']}, users={lp['users']}, views={lp['views']}")

                    # Search Console summary + deltas
                    if gs:
                        lines.append("\n--- SEARCH CONSOLE OVERVIEW ---")
                        lines.append(f"  Total Clicks: {gs.get('total_clicks')}{_fmt_pct(gsc_change.get('clicks') if gsc_change else None)}")
                        lines.append(f"  Total Impressions: {gs.get('total_impressions')}{_fmt_pct(gsc_change.get('impressions') if gsc_change else None)}")
                        lines.append(f"  Average CTR: {gs.get('average_ctr')}{_fmt_pct(gsc_change.get('ctr') if gsc_change else None)}")
                        lines.append(f"  Average Position: {gs.get('average_position')}{_fmt_pct(gsc_change.get('position') if gsc_change else None)} (lower is better)")

                    # Search Console detail tables
                    if gsc_data:
                        tq = (gsc_data.get('top_queries') or {}).get('rows', [])
                        if tq:
                            lines.append("\n--- TOP 10 SEARCH QUERIES ---")
                            for r in tq[:10]:
                                lines.append(f"  '{r[0]}' — clicks={r[1]}, impressions={r[2]}, CTR={r[3]}, pos={r[4]}")
                        tp = (gsc_data.get('top_pages') or {}).get('rows', [])
                        if tp:
                            lines.append("\n--- TOP 10 LANDING PAGES (Search) ---")
                            for r in tp[:10]:
                                lines.append(f"  {r[0]} — clicks={r[1]}, impressions={r[2]}, CTR={r[3]}, pos={r[4]}")
                        cd = (gsc_data.get('country_data') or {}).get('rows', [])
                        if cd:
                            lines.append("\n--- TOP COUNTRIES (Search, by clicks) ---")
                            for r in cd[:8]:
                                lines.append(f"  {r[0]} — clicks={r[1]}, impressions={r[2]}, pos={r[4]}")
                        dd = (gsc_data.get('device_data') or {}).get('rows', [])
                        if dd:
                            lines.append("\n--- DEVICE (Search) ---")
                            for r in dd:
                                lines.append(f"  {r[0]} — clicks={r[1]}, impressions={r[2]}, CTR={r[3]}, pos={r[4]}")

                    # GMB
                    if gmb_data:
                        lines.append("\n--- GOOGLE BUSINESS PROFILE ---")
                        if gmb_data.get('title'):
                            lines.append(f"  Listing: {gmb_data['title']}")
                        for k in ['calls', 'website_clicks', 'directions', 'bookings', 'conversations', 'impressions']:
                            if k in gmb_data:
                                lines.append(f"  {k.replace('_', ' ').title()}: {gmb_data[k]}")

                    # GMC
                    if gmc_data:
                        lines.append("\n--- GOOGLE MERCHANT CENTER ---")
                        lines.append(f"  Merchant: {gmc_data.get('merchant_name', 'n/a')}")
                        if 'clicks' in gmc_data:
                            lines.append(f"  Shopping Clicks: {gmc_data.get('clicks')}")
                        if 'impressions' in gmc_data:
                            lines.append(f"  Shopping Impressions: {gmc_data.get('impressions')}")
                        if 'products' in gmc_data:
                            lines.append(f"  Active Products (approx): {gmc_data.get('products')}")

                    full_data = "\n".join(lines)

                    system_prompt = (
                        "You are a senior digital marketing strategist for DigiRocket "
                        "Technologies. Read the data block and produce a SHORT, "
                        "executive-style closing summary for the client — the kind of "
                        "concise wrap-up that fits at the END of a monthly report.\n\n"
                        "HARD RULES:\n"
                        "1. Every observation must be backed by a specific number from "
                        "the data. NEVER fabricate a metric, query, page, or channel.\n"
                        "2. Be CONCISE — total output must stay under ~180 words. No "
                        "filler, no generic SEO advice that could apply to any site.\n"
                        "3. Plain text only. NO markdown ('#', '**', '*'). Section names "
                        "appear as UPPERCASE LINES followed by a colon. Bullets use '- '.\n\n"
                        "OUTPUT FORMAT (use these EXACT 3 section names, in order):\n\n"
                        "SUMMARY:\n"
                        "2 to 3 sentences synthesizing the period. Lead with the single "
                        "most consequential trend (good or bad), include period-over-period "
                        "change if the data shows it.\n\n"
                        "KEY HIGHLIGHTS:\n"
                        "3 to 4 short bullets ('- '). The most important wins AND issues, "
                        "each citing one specific number from the data.\n\n"
                        "RECOMMENDED NEXT STEPS:\n"
                        "3 to 4 short bullets ('- '). Concrete actions for next month, "
                        "each tied to a specific metric to improve. No fluff."
                    )

                    user_prompt = (
                        f"Analyze the report data below for {ga4_property_name} "
                        f"({period_label}) and write the SHORT closing summary per the "
                        f"format above. Use only the numbers in the data block — do not "
                        f"invent queries, pages, or channels. Stay under 180 words total.\n\n"
                        f"=== REPORT DATA ===\n{full_data}\n=== END REPORT DATA ==="
                    )

                    ai_text = ai_summary(user_prompt, system=system_prompt)
                except Exception as e:
                    print(f"AI insights error: {e}")
                    ai_text = ''

            # Is there a previously-generated PDF on disk? If yes, the header
            # shows a persistent "Open Last PDF" button so the user never
            # loses track of where their report went.
            _report_pdf_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                'static', 'images', 'analytics_report.pdf')
            report_exists = os.path.exists(_report_pdf_path)
            report_pdf_url = url_for('static', filename='images/analytics_report.pdf') if report_exists else None

            _ctx = dict(
                ga4_property_name=ga4_property_name,
                gsc_site_url=gsc_site_url,
                start_date=start_date.strftime('%Y-%m-%d'),
                end_date=end_date.strftime('%Y-%m-%d'),
                ga4_data=ga4_data,
                gsc_data=gsc_data,
                metric_groups=metric_groups,
                compare=compare,
                prev_label=prev_label,
                ov_change=ov_change,
                gsc_change=gsc_change,
                acquisition=acquisition,
                ga4_extra=ga4_extra,
                ga4_daily=ga4_daily,
                indexed_pages=indexed_pages,
                ai_text=ai_text,
                gmb_data=gmb_data,
                gmc_data=gmc_data,
                session_info=session_info,
                report_exists=report_exists,
                report_pdf_url=report_pdf_url,
                # Drives the PMO Portal entry in the user dropdown - only
                # admins should see that menu item, same as on /dashboard.
                is_admin=is_pmo_admin(),
            )
            # Stash for the 5-min fast-path so a refresh / back-button hit
            # skips the entire API + AI pipeline.
            _CD_CACHE[_cd_key] = (_time.time() + _CD_TTL, _ctx)
            return render_template('combined_data.html', **_ctx)

        except Exception as e:
            print(f"Error in view_combined_data: {e}")
            flash(f'Error loading analytics data: {str(e)}', 'error')
            return redirect(url_for('dashboard'))

    @app.route('/session_status')
    def session_status():
        """JSON API used by background pollers (notification bell etc.)
        to check whether the current session is still authenticated.
        Human-facing 'User Info' page is /user-info."""
        if is_authenticated() and refresh_token_if_needed():
            session_info = get_session_info()
            return jsonify({
                'authenticated': True,
                'session_info': session_info
            })
        else:
            return jsonify({
                'authenticated': False,
                'session_info': None
            })

    @app.route('/user-info')
    def user_info():
        """Human-facing page that renders the signed-in user's profile and
        session details as a real HTML page (the raw JSON dump from
        /session_status was being shown to users by mistake)."""
        if not is_authenticated():
            flash('Please log in to view your profile.', 'warning')
            return redirect(url_for('login'))
        return render_template('session_status.html',
                               session_info=get_session_info())

    @app.route('/save-screenshots', methods=['POST'])
    def save_screenshots():
        if not is_authenticated():
            return jsonify({
                'success': False,
                'message': 'Authentication required',
                'redirect': url_for('login')
            }), 401

        try:
            # Create images directory if it doesn't exist
            image_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'images')
            if not os.path.exists(image_dir):
                os.makedirs(image_dir)

            # Create a folder with timestamp for this session
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            session_dir = os.path.join(image_dir, f'session_{timestamp}')
            os.makedirs(session_dir)

            # Get screenshots from request
            data = request.get_json()
            screenshots = data.get('screenshots', [])
            ai_text = (data.get('ai_text') or '').strip()

            # Decode + save each screenshot in parallel. Pillow's PNG encode
            # releases the GIL for the slow bits, so threads give a real win
            # when 20+ screenshots arrive in one POST.
            from concurrent.futures import ThreadPoolExecutor as _TPE_save

            def _decode_and_save(screenshot):
                image_data = screenshot['data'].split(',', 1)[1]
                image_bytes = base64.b64decode(image_data)
                image = Image.open(io.BytesIO(image_bytes))
                filename = f"{screenshot['name']}.png"
                filepath = os.path.join(session_dir, filename)
                image.save(filepath, 'PNG')
                return filepath, filename, screenshot.get('table')

            saved_files = []
            tables_by_image = {}
            if screenshots:
                # Cap at 2 by default: each worker holds a full decoded PIL
                # image in memory; on Render's free tier (~512 MB) a higher
                # cap was OOM-killing the build with 20+ screenshots.
                # Bump via PDF_DECODE_WORKERS on bigger plans.
                try:
                    _decode_workers = max(1, int(os.getenv('PDF_DECODE_WORKERS', '2')))
                except ValueError:
                    _decode_workers = 2
                with _TPE_save(max_workers=min(_decode_workers, len(screenshots))) as _ex:
                    for filepath, filename, tbl in _ex.map(_decode_and_save, screenshots):
                        saved_files.append(filepath)
                        if tbl and (tbl.get('headers') or tbl.get('rows')):
                            tables_by_image[filename] = tbl

            # Create a metadata file with timestamp and file information
            metadata_path = os.path.join(session_dir, 'metadata.txt')
            with open(metadata_path, 'w') as f:
                f.write(f'Screenshot session: {timestamp}\n')
                f.write(f'User: {session.get("user_email", "Unknown")}\n')
                f.write(f'Number of screenshots: {len(saved_files)}\n')
                f.write('\nFiles:\n')
                for file in saved_files:
                    f.write(f'- {os.path.basename(file)}\n')

            # Build slide images directly (no PyPDF2 merge -> no blank slides)
            script_dir = os.path.dirname(os.path.abspath(__file__))
            TEMPLATE_PDF = os.path.join(script_dir, 'Template.pdf')
            START_PAGE = 2  # first 2 template pages are the cover

            # Clear old slides so nothing stale leaks into the report
            aivideo_dir = os.path.join(image_dir, 'AIVideo')
            if os.path.exists(aivideo_dir):
                shutil.rmtree(aivideo_dir)
            os.makedirs(aivideo_dir)

            # Render each slide straight to an image (HD, reliable). Pass the
            # AI Strategy Memo text along so the manifest can mark the AI
            # slide as 'ai-text' — the PPT builder then renders editable text
            # boxes for that slide instead of the captured screenshot.
            build_slide_images(TEMPLATE_PDF, session_dir, aivideo_dir, START_PAGE,
                               ai_text=ai_text, tables_by_image=tables_by_image)

            # Build the downloadable PDF report from the slides
            report_pdf = os.path.join(image_dir, "analytics_report.pdf")
            build_pdf_from_images(aivideo_dir, report_pdf)
            # (The editable PPTX is built on demand from the live data in
            #  /download-report-pptx, so it has real text/tables - not images.)

            # Activity feed: a report was generated
            who = session.get('user_name') or session.get('user_email') or 'Someone'
            db.log_activity('report_generated', f'{who} generated a PDF report',
                            url_for('display_report'), session.get('user_email'))

            # Show the PDF report page (video/voice removed)
            return jsonify({'success': True, 'redirect': url_for('display_report')})

        except Exception as e:
            # Log the error (you should configure proper logging)
            print(f'Error saving screenshots: {str(e)}')
            return jsonify({
                'success': False,
                'message': 'Error saving screenshots',
                'error': str(e)
            }), 500

    @app.route('/list-screenshot-sessions', methods=['GET'])
    def list_sessions():
        if not is_authenticated():
            return jsonify({
                'success': False,
                'message': 'Authentication required'
            }), 401

        try:
            image_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'images')
            if not os.path.exists(image_dir):
                return jsonify({
                    'sessions': []
                })

            sessions = []
            for session_dir in os.listdir(image_dir):
                if session_dir.startswith('session_'):
                    session_path = os.path.join(image_dir, session_dir)
                    if os.path.isdir(session_path):
                        # Get session info
                        timestamp = session_dir.replace('session_', '')
                        num_files = len([f for f in os.listdir(session_path) if f.endswith('.png')])

                        sessions.append({
                            'id': session_dir,
                            'timestamp': timestamp,
                            'num_screenshots': num_files
                        })

            return jsonify({
                'sessions': sorted(sessions, key=lambda x: x['timestamp'], reverse=True)
            })

        except Exception as e:
            return jsonify({
                'success': False,
                'message': 'Error listing sessions',
                'error': str(e)
            }), 500

    @app.route('/display_video/<filename>')
    def display_video(filename):
        if not is_authenticated():
            flash('Please log in to view videos.', 'warning')
            return redirect(url_for('login'))
            
        video_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'images', filename)
        if not os.path.exists(video_path):
            flash('Video not found.', 'error')
            return redirect(url_for('dashboard'))
            
        # Get session info for display
        session_info = get_session_info()
        
        return render_template('video_display.html',
                             video_url=url_for('static', filename=f'images/{filename}'),
                             session_info=session_info)

    @app.route('/report')
    def display_report():
        """Show the generated PDF report (download + preview)."""
        if not is_authenticated():
            flash('Please log in to view the report.', 'warning')
            return redirect(url_for('login'))

        pdf_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'static', 'images', 'analytics_report.pdf')
        # Allow the page to load even without a generated report: the user may
        # have come here from the "Send Email" shortcut just to compose a mail
        # with their own attachments. The email-send endpoint already handles
        # include_report=false gracefully, and the template hides the PDF
        # preview / download panel when has_report is False.
        has_report = os.path.exists(pdf_path)

        # PMO admins get a client dropdown in the email modal so the send can
        # be logged against a specific client. Non-admins just send (no logging).
        clients = []
        if is_pmo_admin():
            try:
                clients = db.list_clients()
            except Exception as e:
                print(f'Error loading clients for report modal: {e}')

        # Editable PPTX is built on demand (native text + tables) when this link
        # is clicked - only offer it if we still know which report this is for.
        pptx_url = url_for('download_report_pptx') if session.get('report_ctx') else None

        # Slide images (page_1.png ...) for the in-browser slide editor.
        aivideo_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   'static', 'images', 'AIVideo')
        slide_images = []
        if os.path.isdir(aivideo_dir):
            import re as _re

            def _pn(name):
                m = _re.search(r'page_(\d+)', name)
                return int(m.group(1)) if m else 0

            files = sorted([f for f in os.listdir(aivideo_dir) if f.lower().endswith('.png')], key=_pn)
            slide_images = [url_for('static', filename=f'images/AIVideo/{f}') for f in files]

        return render_template('report_display.html',
                               pdf_url=url_for('static', filename='images/analytics_report.pdf') if has_report else '',
                               pptx_url=pptx_url if has_report else None,
                               has_report=has_report,
                               clients=clients,
                               slide_images=slide_images if has_report else [],
                               session_info=get_session_info())

    @app.route('/download-report-pptx')
    def download_report_pptx():
        """Build and download the editable PowerPoint.

        Every slide mirrors the PDF: the visual content (chart / table
        screenshot) stays as a MOVABLE static image so the underlying numbers
        can't be tampered with, while the title above and the description
        below it are real, editable PowerPoint text boxes. This is what the
        PMO team needs - they can rewrite the narrative around each slide
        without ever altering the captured data.

        Reads `static/images/AIVideo/manifest.json` (written by
        `build_slide_images` during PDF generation) so we know which slide
        is a template page vs a data slide, and where the original screenshot
        + AI description live for each data slide.
        """
        if not is_authenticated() or not refresh_token_if_needed():
            flash('Please log in.', 'warning')
            return redirect(url_for('login'))

        base = os.path.dirname(os.path.abspath(__file__))
        aivideo_dir = os.path.join(base, 'static', 'images', 'AIVideo')
        out_path = os.path.join(base, 'static', 'images', 'analytics_report.pptx')

        if not os.path.isdir(aivideo_dir) or not any(
                f.lower().endswith('.png') for f in os.listdir(aivideo_dir)):
            flash('Generate the PDF report first - the PPT is built from those slides.', 'error')
            return redirect(url_for('dashboard'))

        try:
            build_editable_image_pptx(aivideo_dir, out_path)
            return send_file(
                out_path, as_attachment=True,
                download_name='analytics_report.pptx',
                mimetype='application/vnd.openxmlformats-officedocument.presentationml.presentation',
            )
        except Exception as e:
            print(f'Error building editable PPTX: {e}')
            flash(f'Could not build PPT: {e}', 'error')
            return redirect(url_for('display_report'))

    @app.route('/download-report-pptx-native')
    def download_report_pptx_native():
        """LEGACY: native-tables deck (everything editable incl. numbers).

        Kept for backwards compatibility / power users who want raw editable
        tables. Re-fetches GA4 + Search Console data using the params saved
        in the session when the combined view was opened.
        """
        if not is_authenticated() or not refresh_token_if_needed():
            flash('Please log in.', 'warning')
            return redirect(url_for('login'))

        ctx = session.get('report_ctx')
        if not ctx:
            flash('Open a report (View Combined Data) first, then download the PPT.', 'error')
            return redirect(url_for('dashboard'))

        try:
            start_date, end_date = validate_dates(ctx['start'], ctx['end'])

            base = os.path.dirname(os.path.abspath(__file__))
            out_path = os.path.join(base, 'static', 'images', 'analytics_report.pptx')

            ga4_data = None
            if ctx.get('ga4_property_id'):
                ga4_data = get_ga4_data(session['access_token'], ctx['ga4_property_id'], start_date, end_date)

            gsc_data = None
            if ctx.get('gsc_site'):
                credentials = Credentials(
                    token=session['access_token'], refresh_token=session.get('refresh_token'),
                    token_uri='https://oauth2.googleapis.com/token',
                    client_id=CLIENT_ID, client_secret=CLIENT_SECRET, scopes=SCOPES)
                gsc_data = get_gsc_detailed_data(credentials, ctx['gsc_site'], start_date, end_date)

            tables = []
            overview = (ga4_data or {}).get('overview') or {}
            OVLABELS = {
                'new_users': 'New Users', 'active_users': 'Active Users', 'returning_users': 'Returning Users',
                'sessions': 'Sessions', 'bounce_rate': 'Bounce Rate',
                'avg_engagement_per_session': 'Avg Engagement / Session', 'views': 'Views', 'event_count': 'Event Count'}

            def _ovdisp(k):
                if k == 'bounce_rate':
                    return f"{overview.get('bounce_rate', 0)}%"
                if k == 'avg_engagement_per_session':
                    return overview.get('avg_engagement_per_session', '0s')
                return f"{overview.get(k, 0):,}"

            selected_m = ctx.get('metrics') or ['new_users', 'active_users', 'returning_users', 'sessions']
            ov_change_p = {}
            if ctx.get('compare') and ctx.get('ga4_property_id'):
                length = (end_date - start_date).days + 1
                p_end = start_date - timedelta(days=1)
                p_start = p_end - timedelta(days=length - 1)
                prev_ov = get_ga4_overview(session['access_token'], ctx['ga4_property_id'], p_start, p_end)
                for k in selected_m:
                    ck = 'avg_engagement_seconds' if k == 'avg_engagement_per_session' else k
                    cur, prev = overview.get(ck), prev_ov.get(ck)
                    try:
                        ov_change_p[k] = round((float(cur) - float(prev)) / float(prev) * 100, 1) if prev else None
                    except (TypeError, ValueError):
                        ov_change_p[k] = None
            metrics = [(OVLABELS.get(k, k), _ovdisp(k), ov_change_p.get(k)) for k in selected_m]

            # User Acquisition (by channel) comparison table
            try:
                acq_ps = acq_pe = None
                if ctx.get('compare'):
                    _len = (end_date - start_date).days + 1
                    acq_pe = start_date - timedelta(days=1)
                    acq_ps = acq_pe - timedelta(days=_len - 1)
                acq = get_ga4_acquisition(session['access_token'], ctx['ga4_property_id'],
                                          start_date, end_date, acq_ps, acq_pe) if ctx.get('ga4_property_id') else []

                def _fc(v):
                    if v is None:
                        return '-'
                    return ('+' if v >= 0 else '') + str(v) + '%'

                if acq:
                    acq_rows = []
                    for ch in acq:
                        cur = ch['current']
                        prev = ch.get('previous') or {}
                        chg = ch.get('change') or {}
                        acq_rows.append([ch['channel'], 'Current', f"{cur.get('total_users',0):,}",
                                         f"{cur.get('new_users',0):,}", f"{cur.get('returning_users',0):,}",
                                         f"{cur.get('sessions',0):,}", f"{cur.get('event_count',0):,}"])
                        if prev:
                            acq_rows.append(['', 'Previous', f"{prev.get('total_users',0):,}",
                                             f"{prev.get('new_users',0):,}", f"{prev.get('returning_users',0):,}",
                                             f"{prev.get('sessions',0):,}", f"{prev.get('event_count',0):,}"])
                            acq_rows.append(['', '% change', _fc(chg.get('total_users')), _fc(chg.get('new_users')),
                                             _fc(chg.get('returning_users')), _fc(chg.get('sessions')), _fc(chg.get('event_count'))])
                    tables.append({'title': 'User Acquisition (by Channel)',
                                   'headers': ['Channel', 'Period', 'Total Users', 'New Users', 'Returning', 'Sessions', 'Events'],
                                   'rows': acq_rows})
            except Exception as e:
                print(f'PPT acquisition table error: {e}')

            if ga4_data:
                cm = ga4_data.get('countryMetrics') or []
                if cm:
                    tables.append({'title': 'GA4 - Geographic Distribution',
                                   'headers': ['Country', 'Users', 'New Users', 'Engaged Sessions', 'Engagement Rate'],
                                   'rows': [[c['country'], f"{c['users']:,}", f"{c['newUsers']:,}", c['engagedSessions'], c['engagementRate']] for c in cm]})
                pm = ga4_data.get('pageMetrics') or []
                if pm:
                    tables.append({'title': 'GA4 - Top Pages',
                                   'headers': ['Page', 'Views', 'Users', 'Avg Duration'],
                                   'rows': [[p['page'], f"{p['views']:,}", f"{p['users']:,}", p['avgDuration']] for p in pm]})
                em = ga4_data.get('eventMetrics') or []
                if em:
                    tables.append({'title': 'GA4 - Top Events',
                                   'headers': ['Event', 'Count', 'Per User'],
                                   'rows': [[e['event'], f"{e['count']:,}", f"{e['perUser']:.2f}"] for e in em]})
                chm = ga4_data.get('channelMetrics') or []
                if chm:
                    tables.append({'title': 'GA4 - Acquisition Channels',
                                   'headers': ['Source', 'New Users', '% of Total', 'Sessions/User', 'Avg Engagement'],
                                   'rows': [[c['source'], f"{c['newUsers']:,}", c['percentage'], f"{c['sessionsPerUser']:.2f}", c['avgEngagementTime']] for c in chm]})
            if gsc_data:
                summ = gsc_data.get('summary') or {}
                gsc_rows = [['Current', f"{summ.get('total_clicks', 0):,}", f"{summ.get('total_impressions', 0):,}",
                             str(summ.get('average_ctr', '')), str(summ.get('average_position', ''))]]
                if ctx.get('compare') and acq_ps and acq_pe:
                    psum = get_gsc_summary(credentials, ctx['gsc_site'], acq_ps, acq_pe) if ctx.get('gsc_site') else {}
                    if psum:
                        cur_clicks = summ.get('total_clicks', 0); cur_impr = summ.get('total_impressions', 0)
                        cur_ctr = float(str(summ.get('average_ctr', '0')).replace('%', '') or 0)
                        cur_pos = float(str(summ.get('average_position', '0')) or 0)

                        def _fc2(v):
                            if v is None:
                                return '-'
                            return ('+' if v >= 0 else '') + str(v) + '%'

                        def _pc(c, p):
                            try:
                                return round((c - p) / p * 100, 1) if p else None
                            except (TypeError, ZeroDivisionError):
                                return None
                        gsc_rows.append(['Previous', f"{psum.get('clicks', 0):,}", f"{psum.get('impressions', 0):,}",
                                         f"{psum.get('ctr', 0)}%", str(psum.get('position', 0))])
                        gsc_rows.append(['% change', _fc2(_pc(cur_clicks, psum.get('clicks', 0))),
                                         _fc2(_pc(cur_impr, psum.get('impressions', 0))),
                                         _fc2(_pc(cur_ctr, psum.get('ctr', 0))),
                                         _fc2(_pc(psum.get('position', 0), cur_pos))])
                tables.append({'title': 'Search Console - Overview',
                               'headers': ['Period', 'Clicks', 'Impressions', 'Avg CTR', 'Avg Position'],
                               'rows': gsc_rows})
                for key, title in [('daily_metrics', 'Daily Performance'), ('top_queries', 'Top Queries'),
                                   ('top_pages', 'Top Pages'), ('country_data', 'Country'), ('device_data', 'Device')]:
                    blk = gsc_data.get(key) or {}
                    if blk.get('rows'):
                        tables.append({'title': f'Search Console - {title}',
                                       'headers': blk.get('headers', []), 'rows': blk['rows']})

            # Overview is shown as metric cards above; only keep a daily LINE chart
            # (single metric over time) - mixed-scale bar charts hid small values.
            charts = []
            if ga4_data and ga4_data.get('rows'):
                rows = ga4_data['rows']
                charts.append({'title': 'Daily Page Views', 'kind': 'line',
                               'categories': [r[0] for r in rows], 'values': [r[1] for r in rows]})

            context = {
                'title': 'Analytics Report',
                'subtitle': f"{ctx.get('ga4_property_name', '')}  |  {ctx['start']} to {ctx['end']}",
                'metrics': metrics,
                'charts': charts,
                'tables': tables,
            }
            base = os.path.dirname(os.path.abspath(__file__))
            out_path = os.path.join(base, 'static', 'images', 'analytics_report.pptx')
            # Native editable deck (no full-slide images) so text/tables/charts are all editable
            build_editable_pptx(context, out_path)
            return send_file(out_path, as_attachment=True, download_name='analytics_report.pptx',
                             mimetype='application/vnd.openxmlformats-officedocument.presentationml.presentation')
        except Exception as e:
            print(f'Error building editable PPTX: {e}')
            flash(f'Could not build PPT: {e}', 'error')
            return redirect(url_for('display_report'))

    @app.route('/save-edited-report', methods=['POST'])
    def save_edited_report():
        """Overwrite the report PDF with the user's edited slides (from the
        in-browser editor), so the preview AND the emailed report use the edits."""
        if not is_authenticated():
            return jsonify({'success': False, 'message': 'Please log in.'}), 401
        try:
            data = request.get_json(silent=True) or {}
            slides = data.get('slides') or []
            if not slides:
                return jsonify({'success': False, 'message': 'Nothing to save.'}), 400

            base = os.path.dirname(os.path.abspath(__file__))
            aivideo_dir = os.path.join(base, 'static', 'images', 'AIVideo')
            os.makedirs(aivideo_dir, exist_ok=True)

            # Replace the old slide images with the edited ones
            for f in os.listdir(aivideo_dir):
                if f.lower().startswith('page_') and f.lower().endswith('.png'):
                    try:
                        os.remove(os.path.join(aivideo_dir, f))
                    except OSError:
                        pass
            for i, durl in enumerate(slides, start=1):
                b64 = durl.split(',', 1)[1] if ',' in durl else durl
                with open(os.path.join(aivideo_dir, f'page_{i}.png'), 'wb') as fh:
                    fh.write(base64.b64decode(b64))

            report_pdf = os.path.join(base, 'static', 'images', 'analytics_report.pdf')
            build_pdf_from_images(aivideo_dir, report_pdf)

            db.log_activity('report_edited', 'Report edited & saved',
                            url_for('display_report'), session.get('user_email'))
            return jsonify({'success': True,
                            'pdf_url': url_for('static', filename='images/analytics_report.pdf')})
        except Exception as e:
            print(f'Error saving edited report: {e}')
            return jsonify({'success': False, 'message': str(e)}), 500

    @app.route('/send-report-email', methods=['POST'])
    def send_report_email_route():
        """Email the generated PDF report to a client, plus any extra files the
        user attached in the compose modal."""
        if not is_authenticated():
            return jsonify({'success': False, 'message': 'Please log in.'}), 401
        try:
            # Modal now posts multipart/form-data (so extra files can be attached).
            form = request.form if request.form else (request.get_json() or {})
            to_email = (form.get('to_email') or '').strip()
            subject = (form.get('subject') or '').strip()
            message = (form.get('message') or '').strip()
            reply_to = (form.get('from_email') or '').strip() or None
            client_id = (form.get('client_id') or '').strip() or None
            report_period = (form.get('report_period') or '').strip() or None

            if not to_email:
                return jsonify({'success': False, 'message': 'Client email is required.'}), 400

            # Attachments are now MANUAL: only attach the report PDF if the user
            # ticked the box; plus any files they added. We also keep the
            # content_type alongside the bytes so we can re-upload to Supabase
            # Storage afterwards (so the client portal can open the same file).
            include_report = (form.get('include_report') or '').lower() in ('1', 'true', 'yes', 'on')
            attachments = []
            file_records = []   # list of (bytes, filename, content_type)
            if include_report:
                pdf_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        'static', 'images', 'analytics_report.pdf')
                if not os.path.exists(pdf_path):
                    return jsonify({'success': False, 'message': 'No report found. Generate one first.'}), 404
                with open(pdf_path, 'rb') as f:
                    b = f.read()
                attachments.append((b, 'analytics_report.pdf'))
                file_records.append((b, 'analytics_report.pdf', 'application/pdf'))

            for up in request.files.getlist('extra_files'):
                if up and up.filename:
                    b = up.read()
                    if b:
                        safe = secure_filename(up.filename) or 'attachment'
                        attachments.append((b, safe))
                        file_records.append((b, safe, up.content_type or 'application/octet-stream'))

            if not attachments:
                return jsonify({'success': False, 'message': 'Please attach at least one file (or tick "Include the report PDF").'}), 400

            total = sum(len(b) for b, _ in attachments)
            if total > 18 * 1024 * 1024:
                return jsonify({'success': False, 'message': 'Attachments too large (max ~18 MB total).'}), 400

            # Unique subject so each send is a NEW email (Gmail threads same-subject
            # mails together; a precise timestamp makes every send distinct).
            final_subject = subject or 'Your Analytics Report'
            if report_period and report_period.lower() not in final_subject.lower():
                final_subject = f'{final_subject} - {report_period}'
            final_subject = f'{final_subject} ({datetime.now().strftime("%d %b %Y, %I:%M:%S %p")})'

            from email_sender import send_email_with_attachments
            send_email_with_attachments(to_email, final_subject, message, attachments, reply_to=reply_to)

            # Best-effort: upload each file to Supabase Storage so the client
            # portal can open it, then log this send against the chosen client.
            if client_id:
                uploaded_files = []
                for b, fname, ctype in file_records:
                    try:
                        meta = db.upload_report_file(client_id, fname, b, ctype)
                        if meta:
                            uploaded_files.append(meta)
                    except Exception as up_err:
                        print(f'[send-report-email] upload {fname!r} failed: {up_err}')
                db.log_report(client_id, report_period, to_email,
                              subject or 'Your Analytics Report',
                              files=uploaded_files or None)

            # Activity feed: a report was emailed
            period_txt = f' ({report_period})' if report_period else ''
            extra_n = len(attachments) - 1
            extra_txt = f' (+{extra_n} file{"s" if extra_n > 1 else ""})' if extra_n > 0 else ''
            db.log_activity('report_emailed', f'Report emailed to {to_email}{period_txt}{extra_txt}',
                            url_for('display_report'), session.get('user_email'))

            return jsonify({'success': True, 'message': f'Report sent to {to_email}'})
        except Exception as e:
            print(f'Error sending report email: {e}')
            return jsonify({'success': False, 'message': str(e)}), 500

    @app.route('/api/drok-chat-stream', methods=['POST'])
    def api_drok_chat_stream():
        """Streaming version of /api/drok-chat — yields response chunks
        as they arrive from the standalone DROK endpoint so the chat
        widget can show words appearing in real time instead of waiting
        for the full reply. Same auth + payload as the non-streaming
        endpoint; the widget can choose either."""
        if not is_authenticated():
            return jsonify({'success': False, 'message': 'Login required'}), 401
        try:
            import drok
            from flask import Response, stream_with_context
            body = request.get_json(silent=True) or {}
            user_msg = (body.get('message') or '').strip()
            if not user_msg:
                return jsonify({'success': False, 'message': 'Empty message'}), 400
            history = body.get('history')
            if not (isinstance(history, list) and history):
                history = None

            def generate():
                got_any = False
                for chunk in drok.stream_api(user_msg, conversation_context=history):
                    got_any = True
                    yield chunk
                # If streaming produced nothing (API down / error), fall back
                # to the non-streaming reply (which itself falls back to
                # local Ollama if configured).
                if not got_any:
                    fallback = drok.chat_reply(user_msg, conversation_context=history)
                    yield fallback or 'DROK is offline right now. Please try again in a moment.'

            return Response(
                stream_with_context(generate()),
                mimetype='text/plain; charset=utf-8',
                headers={'X-Accel-Buffering': 'no', 'Cache-Control': 'no-cache'},
            )
        except Exception as e:
            print(f'[drok-chat-stream] {e}')
            return jsonify({'success': False, 'message': str(e)}), 500

    @app.route('/api/drok-chat', methods=['POST'])
    def api_drok_chat():
        """Floating DROK chat widget on the dashboard. Accepts a user
        message + optional rolling context (last few turns) and returns
        DROK's reply. Uses the local Ollama daemon — slow on CPU but
        zero-cost for now."""
        if not is_authenticated():
            return jsonify({'success': False, 'message': 'Login required'}), 401
        try:
            import drok
            body = request.get_json(silent=True) or {}
            user_msg = (body.get('message') or '').strip()
            if not user_msg:
                return jsonify({'success': False, 'message': 'Empty message'}), 400
            # Prefer `history` (proper messages list) when the new widget
            # sends it; fall back to flat `context` string for older callers.
            history = body.get('history')
            if isinstance(history, list) and history:
                reply = drok.chat_reply(user_msg, conversation_context=history)
            else:
                ctx = body.get('context') or ''
                reply = drok.chat_reply(user_msg, conversation_context=ctx or None)
            if not reply:
                # Ollama down or model not pulled — friendly fallback
                return jsonify({
                    'success': False,
                    'message': 'DROK is offline right now. Please try again in a moment.',
                }), 503
            return jsonify({'success': True, 'reply': reply})
        except Exception as e:
            print(f'[drok-chat] {e}')
            return jsonify({'success': False, 'message': str(e)}), 500

    @app.route('/api/notifications')
    def api_notifications():
        """Recent activity feed for the notification bell.
        - Admin / team: the global activity feed (every report, every
          query, every client action across the whole app).
        - Client: ONLY their own notifications - reports the team has
          sent them + admin replies in their chat thread."""
        if not is_authenticated():
            return jsonify({'success': False, 'items': []}), 401
        try:
            if session.get('is_client'):
                items = _client_notifications()
            else:
                items = db.list_activities(20)
        except Exception as e:
            print(f'Error loading notifications: {e}')
            items = []
        return jsonify({'success': True, 'items': items})

    def _client_notifications():
        """Build a privacy-safe notification feed for the logged-in client.
        Pulls only the events that belong to them: their incoming reports
        and the team's replies on their chat. Other clients' events never
        appear here."""
        client = _current_client()
        if not client:
            return []
        items = []
        try:
            portal_link = url_for('client_portal')
        except Exception:
            portal_link = '/portal'
        # Recent reports sent to this client
        try:
            for r in (db.client_reports(client['id']) or [])[:10]:
                period = r.get('report_period') or 'Analytics report'
                items.append({
                    'type': 'report_received',
                    'message': f'New report received: {period}',
                    'link': portal_link,
                    'created_at': r.get('sent_at'),
                    'user_email': 'DigiRocket team',
                })
        except Exception as e:
            print(f'[notifs] client reports failed: {e}')
        # Recent team replies in this client's chat
        try:
            msgs = db.client_messages(client['id']) or []
            admin_msgs = [m for m in msgs if m.get('sender_type') == 'admin']
            for m in admin_msgs[-10:]:
                snippet = (m.get('body') or '[attachment]').strip().replace('\n', ' ')
                if len(snippet) > 70:
                    snippet = snippet[:70] + '...'
                items.append({
                    'type': 'reply_received',
                    'message': f'Reply from our team: {snippet}',
                    'link': portal_link,
                    'created_at': m.get('created_at'),
                    'user_email': 'DigiRocket team',
                })
        except Exception as e:
            print(f'[notifs] client messages failed: {e}')
        items.sort(key=lambda x: x.get('created_at') or '', reverse=True)
        return items[:20]

    # ============================================================
    #  CLIENT PORTAL  (clients see their own reports + raise queries)
    # ============================================================
    def _current_client():
        """The client whose email matches the logged-in Google account, or None."""
        email = (session.get('user_email') or '').strip()
        if not email:
            return None
        try:
            return db.get_client_by_email(email)
        except Exception as e:
            print(f'[portal] client lookup failed (non-fatal): {e}')
            return None

    def _process_uploads(query_id, files):
        """Upload a list of FileStorage objects to Supabase and return the
        attachments array. Silently skips files >10 MB with a flash."""
        attachments = []
        for up in files or []:
            if not up or not up.filename:
                continue
            content = up.read()
            if not content:
                continue
            if len(content) > 10 * 1024 * 1024:
                flash(f"'{up.filename}' skipped — over 10 MB.", 'warning')
                continue
            meta = db.upload_attachment(query_id, up.filename, content, up.content_type)
            if meta:
                attachments.append(meta)
            else:
                flash(f"Could not upload '{up.filename}'. "
                      "See server logs for details (the bucket auto-creates on first use).", 'error')
        return attachments

    @app.route('/portal')
    def client_portal():
        """A client's own portal: reports + ONE unified chat with the team."""
        if not is_authenticated() or not refresh_token_if_needed():
            flash('Please log in to access your portal.', 'warning')
            return redirect(url_for('login'))

        if not db.is_configured():
            return render_template('client_portal.html', db_ready=False,
                                   client=None, reports=[], messages=[],
                                   session_info=get_session_info())

        client = _current_client()
        if not client:
            if is_pmo_admin():
                return redirect(url_for('pmo_portal'))
            return render_template('client_portal.html', db_ready=True,
                                   client=None, reports=[], messages=[],
                                   session_info=get_session_info())

        # Note: when the admin disables portal_access_enabled on this client,
        # they STILL see the portal page (so they can chat with the team) —
        # but the dashboard icon shows a "complete payment" popup instead of
        # opening the analytics dashboard, and the reports list is hidden.
        # The actual dashboard route below is the hard gate.

        try:
            # Mark every admin message in this client's queries as "read by
            # client" BEFORE fetching, so the freshly-loaded chat shows the
            # correct read_at state on incoming messages.
            db.mark_messages_read_for_client(client['id'], 'admin')

            reports = db.client_reports(client['id'])
            messages = db.client_messages(client['id'])
            # Hide reports whose files weren't archived (sent before the
            # storage-upload code was added). They're not openable, so
            # showing them only clutters the dropdown with red errors.
            reports = [r for r in reports if r.get('files') and len(r['files']) > 0]
            # "NEW" badge = the client hasn't opened this report yet.
            for r in reports:
                r['is_new'] = not r.get('viewed_at')
            # Unviewed group first, viewed group second; newest first in each.
            unviewed = sorted([r for r in reports if r['is_new']],
                              key=lambda r: r.get('sent_at') or '', reverse=True)
            viewed = sorted([r for r in reports if not r['is_new']],
                            key=lambda r: r.get('sent_at') or '', reverse=True)
            reports = unviewed + viewed
        except Exception as e:
            print(f'Error loading client portal: {e}')
            reports, messages = [], []

        return render_template('client_portal.html', db_ready=True,
                               client=client, reports=reports, messages=messages,
                               session_info=get_session_info())

    @app.route('/portal/report/<report_id>/viewed', methods=['POST'])
    def client_mark_report_viewed(report_id):
        """AJAX: client clicked a report -> stamp it viewed so it stops
        showing as NEW and demotes below unread reports next refresh."""
        if not is_authenticated():
            return jsonify({'success': False, 'message': 'login required'}), 401
        client = _current_client()
        if not client:
            return jsonify({'success': False, 'message': 'not a client'}), 403
        try:
            db.mark_report_viewed(report_id, client['id'])
            return jsonify({'success': True})
        except Exception as e:
            print(f'[portal] mark report viewed failed: {e}')
            return jsonify({'success': False, 'message': str(e)}), 500

    @app.route('/portal/dashboard')
    def client_dashboard():
        """Same Analytics Dashboard the team uses, but filtered to ONLY this
        client's GA4 property + Search Console site (from the clients table).
        Privacy: nothing belonging to other clients ever appears here."""
        if not is_authenticated() or not refresh_token_if_needed():
            flash('Please log in.', 'warning')
            return redirect(url_for('login'))

        client = _current_client()
        if not client:
            if is_pmo_admin():
                # Admins use the full unfiltered dashboard.
                return redirect(url_for('dashboard'))
            flash('Your account is not linked to a client profile.', 'warning')
            return redirect(url_for('client_portal'))
        # Admin can disable dashboard access from PMO; honour that. Send the
        # user back to the chat portal with a friendly message (don't log them
        # out — they should still be able to message the team).
        if client.get('portal_access_enabled') is False:
            flash('Please complete your payment to continue.', 'warning')
            return redirect(url_for('client_portal'))

        raw_ga4 = (client.get('ga4_property_id') or '').strip()
        raw_gsc = (client.get('gsc_property_id') or '').strip()
        # Support comma-separated IDs so one client can have multiple
        # properties / sites listed in the dropdowns.
        allowed_ga4_set = {x.strip() for x in raw_ga4.split(',') if x.strip()}
        allowed_gsc_set = {normalize_gsc_property(x.strip())
                           for x in raw_gsc.split(',') if x.strip()}

        # The before_request hook has already swapped session's access_token
        # to the agency's, so these calls fetch with the agency's permissions.
        agency_ready = bool(db.get_service_credential('agency_refresh_token'))
        try:
            ga4_properties, ga4_error = get_ga4_properties(session)
        except Exception as e:
            ga4_properties, ga4_error = [], str(e)
        try:
            gsc_sites = get_gsc_sites(session)
        except Exception as e:
            gsc_sites = []

        print(f"[portal/dashboard] client={client.get('name')} "
              f"agency_ready={agency_ready} "
              f"ga4_fetched={len(ga4_properties or [])} "
              f"gsc_fetched={len(gsc_sites or [])} "
              f"allowed_ga4={allowed_ga4_set} allowed_gsc={allowed_gsc_set}")

        if not agency_ready:
            flash('Live data fetching is not configured yet — your DigiRocket '
                  'account manager must sign in to the app once so the central '
                  'agency credentials get saved.', 'warning')

        # --- Privacy filter: only the client's own assigned property/site. ---
        if allowed_ga4_set:
            ga4_properties = [p for p in (ga4_properties or [])
                              if str(p.get('property_id', '')).strip() in allowed_ga4_set]
            if not ga4_properties:
                # Synthesize rows so the dropdown still lists the IDs we have
                # on file even if the agency token can't see them yet.
                ga4_properties = [{'property_id': pid,
                                   'display_name': f'Your GA4 Property ({pid})',
                                   'account_name': '', 'property_type': 'GA4'}
                                  for pid in sorted(allowed_ga4_set)]
        else:
            ga4_properties = []
            ga4_error = ga4_error or 'No GA4 property is linked to your account yet. Please contact your account manager.'

        if allowed_gsc_set:
            gsc_sites = [s for s in (gsc_sites or [])
                         if str(s.get('site_url', '')).strip() in allowed_gsc_set]
            if not gsc_sites:
                gsc_sites = [{'site_url': url, 'permission_level': 'siteOwner'}
                             for url in sorted(allowed_gsc_set)]
        else:
            gsc_sites = []

        return render_template(
            'dashboard.html',
            ga4_properties=ga4_properties,
            gsc_sites=gsc_sites,
            ga4_error=ga4_error,
            session_info=get_session_info(),
            is_admin=False,
            client_mode=True,
        )

    @app.route('/portal/settings')
    def client_settings():
        """A read-only profile page for the logged-in client."""
        if not is_authenticated() or not refresh_token_if_needed():
            flash('Please log in.', 'warning')
            return redirect(url_for('login'))
        if not db.is_configured():
            return redirect(url_for('client_portal'))
        client = _current_client()
        if not client:
            if is_pmo_admin():
                return redirect(url_for('pmo_portal'))
            return redirect(url_for('client_portal'))
        return render_template('client_settings.html', client=client,
                               session_info=get_session_info())

    @app.route('/portal/message', methods=['POST'])
    def client_send_message():
        """Single endpoint: client sends a message (text and/or files) to
        the team. Auto-finds or creates the chat thread for this client."""
        if not is_authenticated():
            return redirect(url_for('login'))
        client = _current_client()
        if not client:
            flash('Your account is not linked to a client profile.', 'warning')
            return redirect(url_for('client_portal'))

        body = (request.form.get('message') or '').strip()
        reply_to_id = (request.form.get('reply_to_id') or '').strip() or None
        files = request.files.getlist('files')
        has_files = any(f and f.filename for f in files)
        if not body and not has_files:
            flash('Type a message or attach a file before sending.', 'warning')
            return redirect(url_for('client_portal'))

        try:
            qid = db.get_or_create_thread_query(client['id'], subject='Chat')
            if not qid:
                raise RuntimeError('Could not open chat thread')
            attachments = _process_uploads(qid, files) if has_files else []
            db.add_message(qid, 'client', session.get('user_email'),
                           body, attachments, reply_to_id=reply_to_id)
            # Client wrote -> chat is "open" (awaiting reply).
            db.update_query_status(qid, 'open')
            who = client.get('name') or client.get('email') or 'A client'
            db.log_activity('query_message', f'New message from {who}',
                            url_for('pmo_queries'), session.get('user_email'))
            flash('Message sent.', 'success')
        except Exception as e:
            print(f'[portal] send message failed: {e}')
            flash(f'Could not send your message: {e}', 'error')
        return redirect(url_for('client_portal'))

    # Legacy aliases so older forms / bookmarks still work.
    @app.route('/portal/query', methods=['POST'])
    def client_raise_query():
        return client_send_message()

    # ============================================================
    #  PMO QUERIES DASHBOARD  (admins view & answer client queries)
    # ============================================================
    @app.route('/pmo/queries')
    def pmo_queries():
        """One chat card PER CLIENT, with their full unified message timeline."""
        if not is_authenticated() or not refresh_token_if_needed():
            flash('Please log in.', 'warning')
            return redirect(url_for('login'))
        if not is_pmo_admin():
            return render_template('pmo_denied.html', email=session.get('user_email')), 403
        if not db.is_configured():
            return render_template('pmo_queries.html', db_ready=False, chats=[],
                                   session_info=get_session_info())
        try:
            # Admin just opened this page -> every client message is now read.
            db.mark_all_messages_read('client')
            chats = db.list_chats()
        except Exception as e:
            print(f'Error loading queries: {e}')
            return render_template('pmo_queries.html', db_ready=True, chats=[],
                                   load_error=str(e), session_info=get_session_info())
        return render_template('pmo_queries.html', db_ready=True, chats=chats,
                               session_info=get_session_info())

    @app.route('/pmo/chats/<client_id>/message', methods=['POST'])
    def pmo_send_chat_message(client_id):
        """Admin sends a message into one client's chat (text and/or files),
        optionally updates the status (Answered / Resolved)."""
        if not is_pmo_admin():
            return render_template('pmo_denied.html', email=session.get('user_email')), 403

        body = (request.form.get('message') or '').strip()
        status = (request.form.get('status') or 'answered').strip()
        if status not in ('open', 'answered', 'resolved'):
            status = 'answered'
        reply_to_id = (request.form.get('reply_to_id') or '').strip() or None
        files = request.files.getlist('files')
        has_files = any(f and f.filename for f in files)
        if not body and not has_files:
            flash('Type a message or attach a file before sending.', 'warning')
            return redirect(url_for('pmo_queries'))

        try:
            qid = db.get_or_create_thread_query(client_id, subject='Chat')
            if not qid:
                raise RuntimeError('Could not open chat thread for client')
            attachments = _process_uploads(qid, files) if has_files else []
            db.add_message(qid, 'admin', session.get('user_email'),
                           body, attachments, reply_to_id=reply_to_id)
            db.respond_query(qid, body or '(attachment)', status,
                             session.get('user_email'))
            db.log_activity('query_answered', 'A client query was answered',
                            url_for('client_portal'), session.get('user_email'))
            flash('Message sent — visible on the client portal.', 'success')
        except Exception as e:
            print(f'[pmo] send chat message failed: {e}')
            flash(f'Could not send the message: {e}', 'error')
        return redirect(url_for('pmo_queries'))

    # Legacy aliases: older templates / bookmarks posted to the per-query
    # endpoints. Forward them to the per-client endpoint so nothing breaks.
    @app.route('/pmo/queries/<query_id>/message', methods=['POST'])
    def pmo_send_message(query_id):
        q = db.get_query(query_id)
        cid = q.get('client_id') if q else None
        if not cid:
            flash('Query not found.', 'error')
            return redirect(url_for('pmo_queries'))
        return pmo_send_chat_message(cid)

    @app.route('/pmo/queries/<query_id>/respond', methods=['POST'])
    def pmo_respond_query(query_id):
        return pmo_send_message(query_id)

    # ============================================================
    #  PMO PORTAL  (client database) - admins only
    # ============================================================
    @app.route('/pmo')
    def pmo_portal():
        """PMO team portal: manage clients and see report-send history."""
        if not is_authenticated() or not refresh_token_if_needed():
            flash('Please log in to access the PMO portal.', 'warning')
            return redirect(url_for('login'))
        if not is_pmo_admin():
            return render_template('pmo_denied.html',
                                   email=session.get('user_email')), 403

        if not db.is_configured():
            return render_template('pmo.html', clients=[], db_ready=False,
                                   session_info=get_session_info())

        try:
            clients = db.list_clients()
            latest = db.latest_reports()
            for c in clients:
                c['last_report'] = latest.get(c['id'])
            open_queries = db.count_open_chats()
        except Exception as e:
            print(f'Error loading PMO portal: {e}')
            return render_template('pmo.html', clients=[], db_ready=True,
                                   load_error=str(e), session_info=get_session_info())

        return render_template('pmo.html', clients=clients, db_ready=True,
                               open_queries=open_queries,
                               session_info=get_session_info())

    # ---- Client CRUD (JSON APIs, admin only) ----
    def _client_payload():
        d = request.get_json() or {}
        def clean(v):
            v = (str(v).strip() if v is not None else '')
            return v or None
        billing = clean(d.get('billing_cycle_day'))
        try:
            billing = int(billing) if billing else None
        except ValueError:
            billing = None
        # product_links arrives as an array from the frontend; persist as
        # a JSON string so the DB column can be plain TEXT (no Postgres
        # array / JSONB migration required). list_clients's reader on the
        # template side decodes it back.
        raw_links = d.get('product_links') or []
        if isinstance(raw_links, str):
            raw_links = [raw_links]
        product_links = [str(x).strip() for x in raw_links if str(x).strip()]
        import json as _json
        return {
            'name': clean(d.get('name')),
            'email': clean(d.get('email')),
            'ga4_property_id': clean(d.get('ga4_property_id')),
            'gsc_property_id': normalize_gsc_property(clean(d.get('gsc_property_id'))),
            'nature_of_business': clean(d.get('nature_of_business')),
            'billing_cycle_day': billing,
            'start_date': clean(d.get('start_date')),
            'status': clean(d.get('status')) or 'active',
            'notes': clean(d.get('notes')),
            'tat': clean(d.get('tat')),
            'competitor_website': clean(d.get('competitor_website')),
            'target_seo_website': clean(d.get('target_seo_website')),
            'product_links': _json.dumps(product_links) if product_links else None,
        }

    # ---- Email OTP verification for "Add Client" onboarding ----
    # Flow:
    #   1) Admin types the client's email into the modal -> clicks "Verify"
    #      -> /pmo/api/verify-email/send generates a 6-digit code and emails
    #         it via the existing Brevo sender.
    #   2) Admin asks the client for the code, types it in -> /pmo/api/
    #      verify-email/check marks the email as verified in the session.
    #   3) /pmo/api/clients (add) refuses to save until the submitted email
    #      matches a verified one in the session.
    def _pmo_otp_store():
        store = session.get('_pmo_otp') or {}
        # Lazily drop expired entries so the session dict doesn't grow.
        now = time.time()
        store = {k: v for k, v in store.items()
                 if isinstance(v, dict) and v.get('expires_at', 0) > now}
        session['_pmo_otp'] = store
        return store

    @app.route('/pmo/api/verify-email/send', methods=['POST'])
    def pmo_verify_email_send():
        if not is_pmo_admin():
            return jsonify({'success': False, 'message': 'Not authorized.'}), 403
        body = request.get_json(silent=True) or {}
        email = (body.get('email') or '').strip().lower()
        if not email or '@' not in email:
            return jsonify({'success': False,
                            'message': 'Enter a valid email first.'}), 400
        # 6-digit zero-padded code, 10-minute TTL.
        import secrets
        code = f'{secrets.randbelow(1000000):06d}'
        store = _pmo_otp_store()
        store[email] = {
            'code': code,
            'expires_at': time.time() + 600,
            'verified': False,
            'attempts': 0,
        }
        session['_pmo_otp'] = store
        try:
            from email_sender import send_email_with_attachments
            subject = f'Your DigiRocket onboarding code: {code}'
            html = (f"Your DigiRocket onboarding verification code is:\n\n"
                    f"    {code}\n\n"
                    f"It expires in 10 minutes. If you didn't request this, "
                    f"you can safely ignore the email.")
            send_email_with_attachments(email, subject, html, [])
        except Exception as e:
            print(f'[pmo-otp] send failed for {email}: {e}')
            return jsonify({'success': False,
                            'message': f'Could not send code: {e}'}), 500
        return jsonify({'success': True,
                        'message': f'Code sent to {email}. It expires in 10 minutes.'})

    @app.route('/pmo/api/verify-email/check', methods=['POST'])
    def pmo_verify_email_check():
        if not is_pmo_admin():
            return jsonify({'success': False, 'message': 'Not authorized.'}), 403
        body = request.get_json(silent=True) or {}
        email = (body.get('email') or '').strip().lower()
        code = (body.get('code') or '').strip()
        store = _pmo_otp_store()
        entry = store.get(email)
        if not entry:
            return jsonify({'success': False,
                            'message': 'No code on file. Click Verify to send one.'}), 400
        if entry.get('attempts', 0) >= 5:
            store.pop(email, None)
            session['_pmo_otp'] = store
            return jsonify({'success': False,
                            'message': 'Too many wrong attempts. Send a new code.'}), 429
        entry['attempts'] = entry.get('attempts', 0) + 1
        if code != entry.get('code'):
            session['_pmo_otp'] = store
            return jsonify({'success': False,
                            'message': 'Incorrect code. Try again.'}), 400
        entry['verified'] = True
        session['_pmo_otp'] = store
        return jsonify({'success': True, 'message': 'Email verified.'})

    def _is_email_verified(email):
        if not email:
            return False
        store = _pmo_otp_store()
        entry = store.get(email.strip().lower())
        return bool(entry and entry.get('verified'))

    @app.route('/pmo/api/clients', methods=['POST'])
    def pmo_add_client():
        if not is_pmo_admin():
            return jsonify({'success': False, 'message': 'Not authorized.'}), 403
        try:
            payload = _client_payload()
            if not payload.get('name'):
                return jsonify({'success': False, 'message': 'Client name is required.'}), 400
            # Gate: onboarding requires a verified email.
            email = (payload.get('email') or '').strip()
            if not _is_email_verified(email):
                return jsonify({
                    'success': False,
                    'message': ('Email not verified yet. Click "Verify" next '
                                'to the email field, ask the client for the '
                                'code, and confirm it before onboarding.'),
                }), 400
            row = db.add_client(payload)
            db.log_activity('client_added', f"New client added: {payload['name']}",
                            url_for('pmo_portal'), session.get('user_email'))
            # Burn the OTP so it can't onboard a second client.
            store = _pmo_otp_store()
            store.pop(email.lower(), None)
            session['_pmo_otp'] = store
            return jsonify({
                'success': True,
                'client': row,
                'redirect_url': url_for('pmo_client_welcome',
                                        client_id=(row or {}).get('id', '')),
            })
        except Exception as e:
            print(f'Error adding client: {e}')
            return jsonify({'success': False, 'message': str(e)}), 500

    @app.route('/pmo/clients/<client_id>/welcome')
    def pmo_client_welcome(client_id):
        """Post-onboarding success page shown after Add Client succeeds.
        Confirms the new client, then offers Create Strategy / Back to PMO
        as next actions."""
        if not is_pmo_admin():
            return redirect(url_for('login'))
        try:
            client = db.get_client_by_id(client_id) if hasattr(db, 'get_client_by_id') else None
            if not client:
                # Fallback: scan list_clients (handles older db.py versions).
                for c in (db.list_clients() or []):
                    if str(c.get('id')) == str(client_id):
                        client = c
                        break
        except Exception as e:
            print(f'pmo_client_welcome: load failed: {e}')
            client = None
        if not client:
            flash('Client not found.', 'error')
            return redirect(url_for('pmo_portal'))
        return render_template('pmo_client_welcome.html',
                               client=client,
                               session_info=get_session_info())

    @app.route('/pmo/api/clients/<client_id>', methods=['POST'])
    def pmo_update_client(client_id):
        if not is_pmo_admin():
            return jsonify({'success': False, 'message': 'Not authorized.'}), 403
        try:
            payload = _client_payload()
            if not payload.get('name'):
                return jsonify({'success': False, 'message': 'Client name is required.'}), 400
            row = db.update_client(client_id, payload)
            db.log_activity('client_updated', f"Client updated: {payload['name']}",
                            url_for('pmo_portal'), session.get('user_email'))
            return jsonify({'success': True, 'client': row})
        except Exception as e:
            print(f'Error updating client: {e}')
            return jsonify({'success': False, 'message': str(e)}), 500

    @app.route('/pmo/api/clients/<client_id>/access', methods=['POST'])
    def pmo_toggle_client_access(client_id):
        """Flip the per-client portal_access_enabled flag. When false, the
        client gets bounced from /portal and /portal/dashboard with a
        friendly 'access disabled' message. Existing clients default to ON."""
        if not is_pmo_admin():
            return jsonify({'success': False, 'message': 'Not authorized.'}), 403
        try:
            body = request.get_json(silent=True) or {}
            enabled = bool(body.get('enabled'))
            row = db.update_client(client_id, {'portal_access_enabled': enabled})
            name = (row or {}).get('name', 'client')
            db.log_activity('client_access_toggled',
                            f"Portal access {'enabled' if enabled else 'disabled'} for {name}",
                            url_for('pmo_portal'), session.get('user_email'))
            return jsonify({'success': True, 'enabled': enabled})
        except Exception as e:
            print(f'Error toggling client access: {e}')
            return jsonify({'success': False, 'message': str(e)}), 500

    @app.route('/pmo/api/clients/<client_id>/delete', methods=['POST'])
    def pmo_delete_client(client_id):
        if not is_pmo_admin():
            return jsonify({'success': False, 'message': 'Not authorized.'}), 403
        try:
            existing = db.get_client(client_id)
            db.delete_client(client_id)
            name = (existing or {}).get('name', 'client')
            db.log_activity('client_deleted', f'Client deleted: {name}',
                            url_for('pmo_portal'), session.get('user_email'))
            return jsonify({'success': True})
        except Exception as e:
            print(f'Error deleting client: {e}')
            return jsonify({'success': False, 'message': str(e)}), 500

    # ---- Client profile page (full detail + live data) ----
    @app.route('/pmo/client/<client_id>')
    def pmo_client_detail(client_id):
        if not is_authenticated() or not refresh_token_if_needed():
            flash('Please log in to access the PMO portal.', 'warning')
            return redirect(url_for('login'))
        if not is_pmo_admin():
            return render_template('pmo_denied.html', email=session.get('user_email')), 403
        if not db.is_configured():
            flash('Database not connected.', 'error')
            return redirect(url_for('pmo_portal'))

        client = db.get_client(client_id)
        if not client:
            flash('Client not found.', 'error')
            return redirect(url_for('pmo_portal'))
        reports = db.client_reports(client_id)
        return render_template('client_detail.html', client=client, reports=reports,
                               session_info=get_session_info())

    @app.route('/pmo/client/<client_id>/data')
    def pmo_client_data(client_id):
        """Live GA4 + Search Console quick metrics for one client + date range (JSON)."""
        if not is_authenticated() or not refresh_token_if_needed():
            return jsonify({'success': False, 'message': 'Please log in.'}), 401
        if not is_pmo_admin():
            return jsonify({'success': False, 'message': 'Not authorized.'}), 403
        client = db.get_client(client_id)
        if not client:
            return jsonify({'success': False, 'message': 'Client not found.'}), 404

        try:
            start_date, end_date = validate_dates(request.args.get('start'), request.args.get('end'))
        except Exception:
            return jsonify({'success': False, 'message': 'Invalid dates.'}), 400

        result = {'success': True, 'ga4': None, 'gsc': None, 'ga4_error': None, 'gsc_error': None,
                  'start': start_date.strftime('%Y-%m-%d'), 'end': end_date.strftime('%Y-%m-%d')}

        ga4_id = client.get('ga4_property_id')
        if ga4_id:
            try:
                data = get_ga4_data(session['access_token'], ga4_id, start_date, end_date)
                if data:
                    t = data['totals']
                    result['ga4'] = {
                        'activeUsers': t['totalActiveUsers'],
                        'newUsers': t['totalNewUsers'],
                        'views': t['totalViews'],
                        'events': t['totalEventCount'],
                    }
                else:
                    result['ga4_error'] = 'No data (check GA4 property ID)'
            except Exception as e:
                result['ga4_error'] = str(e)
        else:
            result['ga4_error'] = 'No GA4 property set for this client'

        gsc_url = client.get('gsc_property_id')
        if gsc_url:
            try:
                credentials = Credentials(
                    token=session['access_token'], refresh_token=session.get('refresh_token'),
                    token_uri='https://oauth2.googleapis.com/token',
                    client_id=CLIENT_ID, client_secret=CLIENT_SECRET, scopes=SCOPES)
                data = get_gsc_detailed_data(credentials, gsc_url, start_date, end_date)
                if data:
                    s = data['summary']
                    result['gsc'] = {
                        'clicks': s['total_clicks'],
                        'impressions': s['total_impressions'],
                        'ctr': s['average_ctr'],
                        'position': s['average_position'],
                    }
                else:
                    result['gsc_error'] = 'No data (check Search Console property)'
            except Exception as e:
                result['gsc_error'] = str(e)
        else:
            result['gsc_error'] = 'No Search Console property set for this client'

        return jsonify(result)

# Initialize the Flask app with routes
init_routes(app)

if __name__ == '__main__':
    app.run(debug=True)