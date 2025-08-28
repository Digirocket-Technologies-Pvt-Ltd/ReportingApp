from flask import session
import requests
from datetime import datetime
from google.oauth2.credentials import Credentials
from config import CLIENT_ID, CLIENT_SECRET, REDIRECT_URI, SCOPES

def is_authenticated():
    """Check if user is authenticated by verifying access token exists"""
    return 'access_token' in session and session.get('access_token') is not None

def refresh_token_if_needed():
    """Refresh the access token if it's expired"""
    if not is_authenticated():
        return False
        
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
    if 'access_token' in session:
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