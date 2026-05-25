from flask import Flask, render_template, redirect, url_for, request, jsonify, session, flash, send_file
from auth import is_authenticated, refresh_token_if_needed, logout_user, get_user_info, get_session_info, is_pmo_admin
import db
from ga4 import get_ga4_properties, get_ga4_data, get_property_name
from gsc import get_gsc_sites, get_gsc_detailed_data, normalize_gsc_property
from data_processing import validate_dates
from google.oauth2.credentials import Credentials
from config import CLIENT_ID, CLIENT_SECRET, REDIRECT_URI, SCOPES
import os
import base64
from PIL import Image
import io
from datetime import datetime
import requests
from pdf_processing import build_slide_images, build_pdf_from_images, build_editable_pptx
from werkzeug.utils import secure_filename
import shutil
from image_explanation import explain_image_with_gemini

app = Flask(__name__, static_folder='static')


def init_routes(app):
    @app.after_request
    def add_no_cache_headers(response):
        """Disable browser caching during development so every page reload
        actually hits the server (and prints fresh diagnostics in the terminal)."""
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response

    @app.route('/')
    def index():
        if is_authenticated():
            return redirect(url_for('dashboard'))
        return render_template('index.html')

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
            
            flash('Successfully logged in!', 'success')
            return redirect(url_for('dashboard'))
        except requests.exceptions.RequestException as e:
            flash(f'Login error: {str(e)}', 'error')
            return redirect(url_for('index'))

    @app.route('/dashboard')
    def dashboard():
        if not is_authenticated():
            flash('Please log in to access the dashboard.', 'warning')
            return redirect(url_for('login'))

        if not refresh_token_if_needed():
            flash('Session expired. Please log in again.', 'warning')
            return redirect(url_for('login'))

        try:
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
            flash(f'Error loading dashboard: {str(e)}', 'error')
            return redirect(url_for('index'))

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

        try:
            ga4_property_id = request.args.get('ga4_property')
            gsc_site_url = request.args.get('gsc_site')
            start_date_str = request.args.get('start_date')
            end_date_str = request.args.get('end_date')
            selected_metrics = request.args.getlist('metrics') or [
                'new_users', 'active_users', 'returning_users', 'sessions']

            if not all([ga4_property_id, gsc_site_url, start_date_str, end_date_str]):
                flash('Missing required parameters.', 'error')
                return redirect(url_for('dashboard'))

            start_date, end_date = validate_dates(start_date_str, end_date_str)
            ga4_property_name = get_property_name(session['access_token'], ga4_property_id)

            credentials = Credentials(
                token=session['access_token'],
                refresh_token=session['refresh_token'],
                token_uri='https://oauth2.googleapis.com/token',
                client_id=CLIENT_ID,
                client_secret=CLIENT_SECRET,
                scopes=SCOPES
            )

            ga4_data = get_ga4_data(session['access_token'], ga4_property_id, start_date, end_date)
            gsc_data = get_gsc_detailed_data(credentials, gsc_site_url, start_date, end_date)

            if ga4_data is None and gsc_data is None:
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
            }

            # Get session info for display
            session_info = get_session_info()

            # Split selected overview metrics into infographics of max 4 each
            metric_groups = [selected_metrics[i:i + 4] for i in range(0, len(selected_metrics), 4)]

            return render_template(
                'combined_data.html',
                ga4_property_name=ga4_property_name,
                gsc_site_url=gsc_site_url,
                start_date=start_date.strftime('%Y-%m-%d'),
                end_date=end_date.strftime('%Y-%m-%d'),
                ga4_data=ga4_data,
                gsc_data=gsc_data,
                metric_groups=metric_groups,
                session_info=session_info
            )

        except Exception as e:
            print(f"Error in view_combined_data: {e}")
            flash(f'Error loading analytics data: {str(e)}', 'error')
            return redirect(url_for('dashboard'))

    @app.route('/session_status')
    def session_status():
        """API endpoint to check session status"""
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

            saved_files = []

            for screenshot in screenshots:
                # Get image data and name
                image_data = screenshot['data']
                name = screenshot['name']

                # Remove data:image/png;base64, prefix
                image_data = image_data.split(',')[1]

                # Decode base64 image
                image_bytes = base64.b64decode(image_data)

                # Open image with Pillow
                image = Image.open(io.BytesIO(image_bytes))

                # Save image
                filename = f'{name}.png'
                filepath = os.path.join(session_dir, filename)
                image.save(filepath, 'PNG')

                saved_files.append(filepath)

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

            # Render each slide straight to an image (HD, reliable)
            build_slide_images(TEMPLATE_PDF, session_dir, aivideo_dir, START_PAGE)

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
        if not os.path.exists(pdf_path):
            flash('No report found. Please generate one first.', 'error')
            return redirect(url_for('dashboard'))

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
                               pdf_url=url_for('static', filename='images/analytics_report.pdf'),
                               pptx_url=pptx_url,
                               clients=clients,
                               slide_images=slide_images,
                               session_info=get_session_info())

    @app.route('/download-report-pptx')
    def download_report_pptx():
        """Build and download a fully EDITABLE PowerPoint (native text + tables).

        Re-fetches the report's GA4 + Search Console data using the params saved
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

            metrics, tables = [], []
            if ga4_data:
                t = ga4_data['totals']
                metrics = [
                    ('Active Users', f"{t['totalActiveUsers']:,}"),
                    ('Page Views', f"{t['totalViews']:,}"),
                    ('New Users', f"{t['totalNewUsers']:,}"),
                    ('Total Events', f"{t['totalEventCount']:,}"),
                ]
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
                tables.append({'title': 'Search Console - Summary',
                               'headers': ['Clicks', 'Impressions', 'Avg CTR', 'Avg Position'],
                               'rows': [[f"{summ.get('total_clicks', 0):,}", f"{summ.get('total_impressions', 0):,}",
                                         summ.get('average_ctr', ''), summ.get('average_position', '')]]})
                for key, title in [('daily_metrics', 'Daily Performance'), ('top_queries', 'Top Queries'),
                                   ('top_pages', 'Top Pages'), ('country_data', 'Country'), ('device_data', 'Device')]:
                    blk = gsc_data.get(key) or {}
                    if blk.get('rows'):
                        tables.append({'title': f'Search Console - {title}',
                                       'headers': blk.get('headers', []), 'rows': blk['rows']})

            # Native EDITABLE charts (vertical bars + a daily line) from real data
            overview = (ga4_data or {}).get('overview') or {}
            LABELS = {
                'new_users': 'New Users', 'active_users': 'Active Users', 'returning_users': 'Returning Users',
                'sessions': 'Sessions', 'bounce_rate': 'Bounce Rate (%)',
                'avg_engagement_per_session': 'Avg Eng/Session (s)', 'views': 'Views', 'event_count': 'Event Count'}

            def _mval(k):
                if k == 'avg_engagement_per_session':
                    return overview.get('avg_engagement_seconds', 0)
                return overview.get(k, 0)

            selected = ctx.get('metrics') or ['new_users', 'active_users', 'returning_users', 'sessions']
            charts = []
            groups = [selected[i:i + 4] for i in range(0, len(selected), 4)]
            for gi, group in enumerate(groups, start=1):
                charts.append({'title': f'Overview Infographic {gi}', 'kind': 'bar',
                               'categories': [LABELS.get(k, k) for k in group],
                               'values': [_mval(k) for k in group]})
            if gsc_data and gsc_data.get('summary'):
                s = gsc_data['summary']
                charts.append({'title': 'Search Console Overview', 'kind': 'bar',
                               'categories': ['Clicks', 'Impressions', 'Avg CTR (%)', 'Avg Position'],
                               'values': [s.get('total_clicks', 0), s.get('total_impressions', 0),
                                          float(str(s.get('average_ctr', '0')).replace('%', '') or 0),
                                          float(str(s.get('average_position', '0')) or 0)]})
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
            # ticked the box; plus any files they added.
            include_report = (form.get('include_report') or '').lower() in ('1', 'true', 'yes', 'on')
            attachments = []
            if include_report:
                pdf_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        'static', 'images', 'analytics_report.pdf')
                if not os.path.exists(pdf_path):
                    return jsonify({'success': False, 'message': 'No report found. Generate one first.'}), 404
                with open(pdf_path, 'rb') as f:
                    attachments.append((f.read(), 'analytics_report.pdf'))

            for up in request.files.getlist('extra_files'):
                if up and up.filename:
                    b = up.read()
                    if b:
                        attachments.append((b, secure_filename(up.filename) or 'attachment'))

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

            # Best-effort: log this send against the chosen client in the PMO portal.
            if client_id:
                db.log_report(client_id, report_period, to_email, subject or 'Your Analytics Report')

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

    @app.route('/api/notifications')
    def api_notifications():
        """Recent activity feed for the notification bell (any logged-in user)."""
        if not is_authenticated():
            return jsonify({'success': False, 'items': []}), 401
        try:
            items = db.list_activities(20)
        except Exception as e:
            print(f'Error loading notifications: {e}')
            items = []
        return jsonify({'success': True, 'items': items})

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
        except Exception as e:
            print(f'Error loading PMO portal: {e}')
            return render_template('pmo.html', clients=[], db_ready=True,
                                   load_error=str(e), session_info=get_session_info())

        return render_template('pmo.html', clients=clients, db_ready=True,
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
        }

    @app.route('/pmo/api/clients', methods=['POST'])
    def pmo_add_client():
        if not is_pmo_admin():
            return jsonify({'success': False, 'message': 'Not authorized.'}), 403
        try:
            payload = _client_payload()
            if not payload.get('name'):
                return jsonify({'success': False, 'message': 'Client name is required.'}), 400
            row = db.add_client(payload)
            db.log_activity('client_added', f"New client added: {payload['name']}",
                            url_for('pmo_portal'), session.get('user_email'))
            return jsonify({'success': True, 'client': row})
        except Exception as e:
            print(f'Error adding client: {e}')
            return jsonify({'success': False, 'message': str(e)}), 500

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