from flask import session
import os
import requests
from datetime import datetime
from google.oauth2.credentials import Credentials
from config import CLIENT_ID, CLIENT_SECRET, REDIRECT_URI, SCOPES


# PMO team always logs in with these IDs -> permanent built-in admins
# (full PMO portal + dashboard access). Add more via the PMO_ADMINS env var
# (comma-separated), no code change needed.
DEFAULT_PMO_ADMINS = [
    'analytics@digirocketads.com',                       # agency account (see AGENCY_EMAIL below)
    'nikhar.makkar@digirockettechnologies.com',          # PMO
    'sidharth.anant@digirockettechnologies.com',         # PMO
    'shweta.singh@digirockettechnologies.com',           # PMO
]

# The ONE Google account whose OAuth refresh_token is used as the "agency"
# credential to fetch GA4/GSC data on clients' behalf. Pinning this to a
# single account means other PMO admins logging in will NOT clobber the
# agency token (which would break client dashboards if they lack GA4 access).
# Override with the AGENCY_EMAIL env var if the agency account ever changes.
DEFAULT_AGENCY_EMAIL = 'analytics@digirocketads.com'


def pmo_admins():
    """Whitelisted PMO admin emails (lower-cased): built-in defaults + PMO_ADMINS env."""
    env_admins = [e.strip().lower() for e in (os.getenv('PMO_ADMINS') or '').split(',') if e.strip()]
    defaults = [e.strip().lower() for e in DEFAULT_PMO_ADMINS]
    # de-dupe while keeping it simple
    return list(dict.fromkeys(defaults + env_admins))


def is_pmo_admin():
    """True only if the logged-in user's email is in the PMO_ADMINS whitelist."""
    if not is_authenticated():
        return False
    return (session.get('user_email') or '').lower() in pmo_admins()


def agency_email():
    """The single account whose token powers client GA4/GSC dashboards."""
    return (os.getenv('AGENCY_EMAIL') or DEFAULT_AGENCY_EMAIL).strip().lower()


def is_agency_account():
    """True only for the designated agency Google account. Used to decide
    whose refresh_token gets saved as the shared agency credential."""
    if not is_authenticated():
        return False
    return (session.get('user_email') or '').lower() == agency_email()

def is_authenticated():
    """Check if user is authenticated by verifying access token exists"""
    return 'access_token' in session and session.get('access_token') is not None

def refresh_token_if_needed():
    """Refresh the access token if it's expired"""
    if not is_authenticated():
        return False

    # Email-OTP (and any non-Google) sessions don't carry a Google token, so
    # there's nothing to refresh against Google — the session is valid as long
    # as it hasn't passed Flask's PERMANENT_SESSION_LIFETIME.
    if (session.get('auth_provider') or 'google') != 'google':
        return True

    # Check if token is expired
    if 'token_expiry' in session and datetime.now().timestamp() > session['token_expiry']:
        if 'refresh_token' not in session:
            return False
            
        try:
            token_url = 'https://oauth2.googleapis.com/token'
            data = {
                'client_id': CLIENT_ID,
                'client_secret': CLIENT_SECRET,
                'refresh_token': session['refresh_token'],
                'grant_type': 'refresh_token'
            }
            response = requests.post(token_url, data=data)
            response.raise_for_status()

            token_data = response.json()
            session['access_token'] = token_data['access_token']
            session['token_expiry'] = datetime.now().timestamp() + token_data['expires_in']
            
            # Update refresh token if a new one is provided
            if 'refresh_token' in token_data:
                session['refresh_token'] = token_data['refresh_token']
                
            return True
        except requests.exceptions.RequestException as e:
            print(f"Error refreshing token: {e}")
            clear_session()
            return False
    return True

def clear_session():
    """Clear all session data"""
    session.clear()

def get_user_info():
    """Get basic user information from Google"""
    if not is_authenticated() or not refresh_token_if_needed():
        return None
        
    try:
        headers = {
            'Authorization': f'Bearer {session["access_token"]}',
            'Content-Type': 'application/json'
        }
        
        response = requests.get(
            'https://www.googleapis.com/oauth2/v2/userinfo',
            headers=headers
        )
        response.raise_for_status()
        
        user_data = response.json()
        # Store user info in session for easy access
        session['user_email'] = user_data.get('email')
        session['user_name'] = user_data.get('name')
        session['user_picture'] = user_data.get('picture')
        
        return user_data
    except requests.exceptions.RequestException as e:
        print(f"Error fetching user info: {e}")
        return None

def logout_user():
    """Logout user by revoking tokens and clearing session"""
    # Only Google-issued tokens can/should be revoked at Google. Email-OTP
    # sessions hold a sentinel token, so just clear the session for those.
    if (session.get('auth_provider') or 'google') == 'google' and 'access_token' in session:
        try:
            # Revoke the access token
            revoke_url = 'https://oauth2.googleapis.com/revoke'
            params = {'token': session['access_token']}
            requests.post(revoke_url, params=params)
        except requests.exceptions.RequestException as e:
            print(f"Error revoking token: {e}")
    
    # Clear all session data
    clear_session()
    return True

def get_session_info():
    """Get current session information for display"""
    if not is_authenticated():
        return None
        
    return {
        'user_email': session.get('user_email'),
        'user_name': session.get('user_name'),
        'user_picture': session.get('user_picture'),
        'login_time': session.get('login_time'),
        'token_expiry': session.get('token_expiry')
    }