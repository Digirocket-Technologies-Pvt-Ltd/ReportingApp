import os

from app import app

if __name__ == '__main__':
    # Allow insecure transport for local OAuth testing (development only)
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
    app.run(debug=True)
