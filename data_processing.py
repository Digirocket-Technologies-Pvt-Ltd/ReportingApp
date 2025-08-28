from datetime import datetime, timedelta
import requests

def format_duration(seconds):
    """Convert seconds to a readable duration format"""
    minutes = int(seconds / 60)
    remaining_seconds = int(seconds % 60)
    return f"{minutes}m {remaining_seconds}s"

def validate_dates(start_date_str, end_date_str):
    """Validate and parse date strings, return default dates if invalid"""
    try:
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d')
        end_date = datetime.strptime(end_date_str, '%Y-%m-%d')

        if end_date > datetime.now():
            end_date = datetime.now()

        if start_date > end_date:
            start_date = end_date - timedelta(days=30)

        return start_date, end_date
    except (ValueError, TypeError):
        end_date = datetime.now()
        start_date = end_date - timedelta(days=30)
        return start_date, end_date
