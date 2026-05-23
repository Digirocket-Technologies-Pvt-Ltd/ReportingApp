"""Send the generated PDF report to a client by email.

Two delivery methods are supported, picked automatically:

1. **Brevo HTTP API** (recommended on Render) — if ``BREVO_API_KEY`` is set,
   the email is sent over HTTPS (port 443). Render's free tier BLOCKS the
   normal SMTP ports (25/465/587), so plain SMTP fails there with
   "[Errno 101] Network is unreachable". HTTPS is never blocked, so this works.

2. **SMTP** (good for local dev) — used when ``BREVO_API_KEY`` is not set.
   .env keys: SMTP_HOST (default smtp.gmail.com), SMTP_PORT (default 587),
   SMTP_USER (sender email), SMTP_PASSWORD (Gmail app password).
"""
import os
import base64
import smtplib
import json
from email.message import EmailMessage

import requests


def _send_via_brevo(to_email, subject, body, pdf_path, reply_to, attachment_name):
    """Send the report through Brevo's transactional email HTTP API (port 443)."""
    api_key = os.getenv('BREVO_API_KEY')
    # Sender MUST be an address you verified in Brevo (Senders & IP page).
    sender_email = os.getenv('BREVO_SENDER', os.getenv('SMTP_USER'))
    sender_name = os.getenv('BREVO_SENDER_NAME', 'DigiRocket')

    if not sender_email:
        raise RuntimeError("BREVO_SENDER (or SMTP_USER) not set - need a verified sender email")

    with open(pdf_path, 'rb') as f:
        pdf_b64 = base64.b64encode(f.read()).decode('ascii')

    payload = {
        'sender': {'email': sender_email, 'name': sender_name},
        'to': [{'email': to_email}],
        'subject': subject or 'Your Analytics Report',
        'textContent': body or 'Hi,\n\nPlease find your analytics report attached.\n\nThanks.',
        'attachment': [{'content': pdf_b64, 'name': attachment_name}],
    }
    if reply_to:
        payload['replyTo'] = {'email': reply_to}

    resp = requests.post(
        'https://api.brevo.com/v3/smtp/email',
        headers={
            'api-key': api_key,
            'content-type': 'application/json',
            'accept': 'application/json',
        },
        data=json.dumps(payload),
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Brevo send failed ({resp.status_code}): {resp.text}")
    return True


def _send_via_smtp(to_email, subject, body, pdf_path, reply_to, attachment_name):
    """Send the report through SMTP (works locally; blocked on Render free tier)."""
    host = os.getenv('SMTP_HOST', 'smtp.gmail.com')
    port = int(os.getenv('SMTP_PORT', '587'))
    user = os.getenv('SMTP_USER')
    password = os.getenv('SMTP_PASSWORD')

    if not user or not password:
        raise RuntimeError("SMTP_USER / SMTP_PASSWORD not set in .env")

    msg = EmailMessage()
    msg['Subject'] = subject or 'Your Analytics Report'
    msg['From'] = user
    msg['To'] = to_email
    if reply_to:
        msg['Reply-To'] = reply_to
    msg.set_content(body or 'Hi,\n\nPlease find your analytics report attached.\n\nThanks.')

    with open(pdf_path, 'rb') as f:
        msg.add_attachment(
            f.read(), maintype='application', subtype='pdf', filename=attachment_name
        )

    with smtplib.SMTP(host, port, timeout=30) as server:
        server.starttls()
        server.login(user, password)
        server.send_message(msg)
    return True


def send_report_email(to_email, subject, body, pdf_path, reply_to=None, attachment_name='analytics_report.pdf'):
    """Send `pdf_path` as an attachment to `to_email`.

    Uses Brevo's HTTP API if BREVO_API_KEY is set (required on Render),
    otherwise falls back to SMTP (fine for local development).
    """
    if not to_email:
        raise ValueError("Recipient (client) email is required")
    if not os.path.exists(pdf_path):
        raise FileNotFoundError("Report PDF not found - generate a report first")

    if os.getenv('BREVO_API_KEY'):
        return _send_via_brevo(to_email, subject, body, pdf_path, reply_to, attachment_name)
    return _send_via_smtp(to_email, subject, body, pdf_path, reply_to, attachment_name)
