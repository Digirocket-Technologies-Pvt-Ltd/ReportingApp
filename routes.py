from flask import Flask, render_template, redirect, url_for, request, jsonify, session, flash
from auth import is_authenticated, refresh_token_if_needed, logout_user, get_user_info, get_session_info
from ga4 import get_ga4_properties, get_ga4_data, get_property_name
from gsc import get_gsc_sites, get_gsc_detailed_data
from data_processing import validate_dates
from google.oauth2.credentials import Credentials
from config import CLIENT_ID, CLIENT_SECRET, REDIRECT_URI, SCOPES
import os
import base64
from PIL import Image
import io
from datetime import datetime
import requests
from pdf_processing import build_slide_images, build_pdf_from_images
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
            # select_account = Google account chooser dikhao (taaki sahi account chun sako)
            # consent = naye permissions ke liye dobara poocho
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
                session_info=session_info
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
        html += "<p>Inhe dashboard ki GA4 dropdown mein select karo (Property ID se pehchaano):</p>"
        for p in with_data:
            html += (f"<div class='ok'><b>{p['name']}</b><br>"
                     f"<span class='id'>Property ID: {p['id']}</span><br>"
                     f"<span class='stats'>{p['users']:,} users &nbsp;|&nbsp; {p['views']:,} page views</span></div>")
        if not with_data:
            html += "<p>⚠️ Kisi bhi property mein last 90 days ka data nahi mila.</p>"
        html += f"<p class='empty'>({len(empty)} properties khaali hain - inme tracking data nahi aa raha)</p>"
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

            # Get session info for display
            session_info = get_session_info()

            return render_template(
                'combined_data.html',
                ga4_property_name=ga4_property_name,
                gsc_site_url=gsc_site_url,
                start_date=start_date.strftime('%Y-%m-%d'),
                end_date=end_date.strftime('%Y-%m-%d'),
                ga4_data=ga4_data,
                gsc_data=gsc_data,
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

        return render_template('report_display.html',
                               pdf_url=url_for('static', filename='images/analytics_report.pdf'),
                               session_info=get_session_info())

    @app.route('/send-report-email', methods=['POST'])
    def send_report_email_route():
        """Email the generated PDF report to a client."""
        if not is_authenticated():
            return jsonify({'success': False, 'message': 'Please log in.'}), 401
        try:
            data = request.get_json() or {}
            to_email = (data.get('to_email') or '').strip()
            subject = (data.get('subject') or '').strip()
            message = (data.get('message') or '').strip()
            reply_to = (data.get('from_email') or '').strip() or None

            if not to_email:
                return jsonify({'success': False, 'message': 'Client email is required.'}), 400

            pdf_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    'static', 'images', 'analytics_report.pdf')
            if not os.path.exists(pdf_path):
                return jsonify({'success': False, 'message': 'No report found. Generate one first.'}), 404

            from email_sender import send_report_email
            send_report_email(to_email, subject, message, pdf_path, reply_to=reply_to)
            return jsonify({'success': True, 'message': f'Report sent to {to_email}'})
        except Exception as e:
            print(f'Error sending report email: {e}')
            return jsonify({'success': False, 'message': str(e)}), 500

# Initialize the Flask app with routes
init_routes(app)

if __name__ == '__main__':
    app.run(debug=True)