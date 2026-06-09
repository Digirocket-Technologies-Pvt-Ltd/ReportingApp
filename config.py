import os
import requests

CLIENT_ID = os.getenv('CLIENT_ID')
CLIENT_SECRET = os.getenv('CLIENT_SECRET')
REDIRECT_URI = os.getenv('REDIRECT_URI')

# Password gate for the SHARED agency Google account (analytics@digirocketads.com).
# The whole company can sign into that Google account, so anyone could otherwise
# "Continue with Google" and land in the reporting app with full agency access.
# After Google auth, if the signed-in email is the agency account, we require
# THIS password too — only the PMO team is told it. Regular staff keep using
# their own email IDs (no gate). Set AGENCY_LOGIN_PASSWORD in the environment
# (Render dashboard + local .env); the fallback is used only if the env is unset.
AGENCY_LOGIN_PASSWORD = os.getenv('AGENCY_LOGIN_PASSWORD') or 'CHANGE_ME_AGENCY_PASSWORD'

SCOPES = [
    'openid',
    'https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/userinfo.profile',
    'https://www.googleapis.com/auth/analytics.readonly',
    'https://www.googleapis.com/auth/webmasters.readonly',
    'https://www.googleapis.com/auth/analytics',
    # Google Business Profile (GMB) - calls, directions, website clicks, impressions
    'https://www.googleapis.com/auth/business.manage',
    # Google Merchant Center (GMC) - product/shopping data (only if an account exists)
    'https://www.googleapis.com/auth/content',
]
