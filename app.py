from flask import Flask
import os
from dotenv import load_dotenv
from routes import init_routes
import requests
from datetime import timedelta

load_dotenv()

app = Flask(__name__)

# Enhanced session configuration
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'your-secret-key-here')
app.config['SESSION_COOKIE_SECURE'] = False  # Set to True in production with HTTPS
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=2)  # Session expires after 2 hours
app.config['SESSION_COOKIE_NAME'] = 'analytics_session'

# Additional security configurations
app.config['WTF_CSRF_ENABLED'] = True
app.config['WTF_CSRF_TIME_LIMIT'] = None

init_routes(app)

if __name__ == '__main__':
    # Only allow insecure transport in development
    if app.debug:
        os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
    app.run(debug=True)