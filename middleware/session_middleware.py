from functools import wraps
from flask import session, redirect, url_for, request, flash, jsonify
from datetime import datetime
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def login_required(f):
    """Decorator to require authentication for routes"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        from auth import is_authenticated, refresh_token_if_needed
        
        if not is_authenticated():
            if request.is_json:
                return jsonify({
                    'success': False,
                    'message': 'Authentication required',
                    'redirect': url_for('login')
                }), 401
            flash('Please log in to access this page.', 'warning')
            return redirect(url_for('login'))
        
        if not refresh_token_if_needed():
            if request.is_json:
                return jsonify({
                    'success': False,
                    'message': 'Session expired',
                    'redirect': url_for('login')
                }), 401
            flash('Your session has expired. Please log in again.', 'warning')
            return redirect(url_for('login'))
        
        return f(*args, **kwargs)
    return decorated_function

def track_session_activity():
    """Track user session activity for security monitoring"""
    if 'user_email' in session:
        session['last_activity'] = datetime.now().timestamp()
        session['request_count'] = session.get('request_count', 0) + 1
        
        # Log activity for security monitoring
        logger.info(f"User activity: {session.get('user_email')} - {request.endpoint} - {request.remote_addr}")

def check_session_security():
    """Check for potential security issues with the session"""
    current_time = datetime.now().timestamp()
    
    # Check for session timeout (2 hours of inactivity)
    if 'last_activity' in session:
        if current_time - session['last_activity'] > 7200:  # 2 hours
            from auth import clear_session
            clear_session()
            return False
    
    # Check for suspicious activity (too many requests)
    if session.get('request_count', 0) > 1000:  # Limit requests per session
        logger.warning(f"Suspicious activity detected for user: {session.get('user_email')}")
        # You could implement additional security measures here
    
    return True

def session_cleanup():
    """Clean up expired or invalid sessions"""
    try:
        if not check_session_security():
            return False
        track_session_activity()
        return True
    except Exception as e:
        logger.error(f"Session cleanup error: {e}")
        return False