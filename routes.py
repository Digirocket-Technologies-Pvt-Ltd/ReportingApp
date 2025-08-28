# DEEPGRAM_API_KEY = "78a09f34ec35a20e35da5e6edd720bafa149dbef"
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
from pdf_processing import process_pdf
from pdf_to_images import convert_pdf_to_images
from image_explanation import explain_image_with_gemini
from text_to_speech import convert_text_to_speech
from sync_media import create_video_from_images_and_audio
from deepgram import DeepgramClient, SpeakOptions
from moviepy.editor import ImageSequenceClip, AudioFileClip, concatenate_videoclips
import re
import time

app = Flask(__name__, static_folder='static')

DEEPGRAM_API_KEY = "dc7ec702a531c88144479248a00c382b71fd4b6f"

def extract_page_number(filename):
    # Extract the page number from the filename using regex
    match = re.search(r'page_(\d+)', filename)
    return int(match.group(1)) if match else None

def create_video_from_images_and_audio(image_folder, audio_folder, output_video):
    print("Starting fade-only video creation process...")
    start_time = time.time()

    # Get list of image and audio files
    print("Scanning directories for image and audio files...")
    print(f"Looking in image folder: {image_folder}")
    print(f"Looking in audio folder: {audio_folder}")

    image_files = [f for f in os.listdir(image_folder) if f.endswith(('.png', '.jpg', '.jpeg'))]
    audio_files = [f for f in os.listdir(audio_folder) if f.endswith(('.mp3', '.wav'))]

    print(f"Found {len(image_files)} image files and {len(audio_files)} audio files")

    # Ensure there is at least one image and one audio file
    if not image_files or not audio_files:
        raise ValueError("No images or audio files found in the specified directories.")

    # Sort files based on the extracted page number
    print("Sorting files by page number...")
    image_files.sort(key=lambda x: extract_page_number(x))
    audio_files.sort(key=lambda x: extract_page_number(x))

    # Create a list to hold video clips
    video_clips = []

    # Loop through images and corresponding audio files
    print("\nStarting to process individual slides:")
    for i, (image_file, audio_file) in enumerate(zip(image_files, audio_files)):
        print(f"Processing slide {i+1}/{len(image_files)}: {image_file}")

        image_path = os.path.join(image_folder, image_file)
        audio_path = os.path.join(audio_folder, audio_file)

        print(f"  Loading image: {image_path}")
        # Load the image and audio
        image_clip = ImageSequenceClip([image_path], durations=[5])

        print(f"  Loading audio: {audio_path}")
        audio_clip = AudioFileClip(audio_path)
        print(f"  Audio duration: {audio_clip.duration:.2f} seconds")

        # Set the duration of the image clip to match the audio clip
        print("  Setting image duration to match audio...")
        image_clip = image_clip.set_duration(audio_clip.duration)

        # Only add fade effects
        print("  Adding fade effects...")
        image_clip = image_clip.fadein(0.5).fadeout(0.5)

        # Combine the image and audio into a video clip
        print("  Combining image and audio...")
        video_clip = image_clip.set_audio(audio_clip)

        video_clips.append(video_clip)
        print(f"  Completed processing slide {i+1}\n")

    # Concatenate all video clips into a single video
    print("Concatenating all video clips...")
    final_video = concatenate_videoclips(video_clips, method="compose")

    # Calculate total duration
    total_duration = sum(clip.duration for clip in video_clips)
    print(f"Total video duration: {total_duration:.2f} seconds")

    # Write the final video to a file
    print(f"Writing final video to {output_video}...")
    final_video.write_videofile(output_video, codec='libx264', fps=24)

    end_time = time.time()
    elapsed_time = end_time - start_time
    print(f"Fade-only video creation completed in {elapsed_time:.2f} seconds")

def init_routes(app):
    @app.route('/')
    def index():
        if is_authenticated():
            return redirect(url_for('dashboard'))
        return render_template('index.html')

    @app.route('/login')
    def login():
        session.clear()
        auth_url = (
            "https://accounts.google.com/o/oauth2/auth"
            f"?client_id={CLIENT_ID}"
            f"&redirect_uri={REDIRECT_URI}"
            "&response_type=code"
            f"&scope={' '.join(SCOPES)}"
            "&access_type=offline"
            "&prompt=consent"
        )
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

            # Trigger PDF processing
            script_dir = os.path.dirname(os.path.abspath(__file__))
            TEMPLATE_PDF = os.path.join(script_dir, 'Template.pdf')

            OUTPUT_PDF = os.path.join(session_dir, "output_with_images.pdf")
            START_PAGE = 2  # Index 2 means starting from page 3 (0-based indexing)
            process_pdf(TEMPLATE_PDF, session_dir, OUTPUT_PDF, START_PAGE)

            # Convert PDF to images and save in AIVideo folder
            aivideo_dir = os.path.join(image_dir, 'AIVideo')
            if not os.path.exists(aivideo_dir):
                os.makedirs(aivideo_dir)
            convert_pdf_to_images(OUTPUT_PDF, aivideo_dir)

            # Define a single explanations directory
            explanations_dir = os.path.join(image_dir, 'Explanations')
            if not os.path.exists(explanations_dir):
                os.makedirs(explanations_dir)

            # Explain each image and save the narration
            for image_file in os.listdir(aivideo_dir):
                if image_file.endswith('.png'):
                    image_path = os.path.join(aivideo_dir, image_file)
                    explain_image_with_gemini(image_path, explanations_dir)

            # Convert explanations to speech
            audio_dir = os.path.join(image_dir, 'AudioExplanations')
            if not os.path.exists(audio_dir):
                os.makedirs(audio_dir)
            for explanation_file in os.listdir(explanations_dir):
                if explanation_file.endswith('.txt'):
                    explanation_path = os.path.join(explanations_dir, explanation_file)
                    convert_text_to_speech(explanation_path, audio_dir)

            # Create video from images and audio
            output_video = os.path.join(image_dir, "output_video_fade_only.mp4")
            create_video_from_images_and_audio(aivideo_dir, audio_dir, output_video)

            # Redirect to the video display page
            video_filename = "output_video_fade_only.mp4"
            return redirect(url_for('display_video', filename=video_filename))

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

    @app.route('/convert-text-to-speech', methods=['POST'])
    def convert_text_to_speech_route():
        if not is_authenticated():
            return jsonify({
                'success': False,
                'message': 'Authentication required'
            }), 401

        try:
            data = request.get_json()
            text = data.get('text')
            filename = data.get('filename', 'audio.mp3')

            if not text:
                return jsonify({
                    'success': False,
                    'message': 'Text is required'
                }), 400

            deepgram = DeepgramClient(DEEPGRAM_API_KEY)
            options = SpeakOptions(model="aura-asteria-en")

            audio_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'AudioExplanations')
            if not os.path.exists(audio_dir):
                os.makedirs(audio_dir)

            filepath = os.path.join(audio_dir, filename)
            response = deepgram.speak.v("1").save(filepath, {"text": text}, options)

            return jsonify({
                'success': True,
                'message': 'Text converted to speech successfully',
                'filepath': filepath
            })

        except Exception as e:
            print(f'Error converting text to speech: {str(e)}')
            return jsonify({
                'success': False,
                'message': 'Error converting text to speech',
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

# Initialize the Flask app with routes
init_routes(app)

if __name__ == '__main__':
    app.run(debug=True)