"""Send emails with a file attachment to a client.

Two delivery methods are picked automatically:

1. **Brevo HTTP API** (recommended on Render) - if ``BREVO_API_KEY`` is set,
   the email is sent over HTTPS (port 443). Render's free tier BLOCKS the
   normal SMTP ports (25/465/587), so plain SMTP fails there with
   "[Errno 101] Network is unreachable". HTTPS is never blocked, so this works.

2. **SMTP** (good for local dev) - used when ``BREVO_API_KEY`` is not set.
   .env keys: SMTP_HOST (default smtp.gmail.com), SMTP_PORT (default 587),
   SMTP_USER (sender email), SMTP_PASSWORD (Gmail app password).
"""
import os
import base64
import smtplib
import json
from email.message import EmailMessage
from email.utils import make_msgid

import requests

_DEFAULT_BODY = 'Hi,\n\nPlease find your report attached.\n\nThanks.'


def _guess_mime(filename):
    """Return (maintype, subtype) for an attachment filename."""
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in (filename or '') else ''
    return {
        'pdf': ('application', 'pdf'),
        'pptx': ('application', 'vnd.openxmlformats-officedocument.presentationml.presentation'),
        'ppt': ('application', 'vnd.ms-powerpoint'),
        'docx': ('application', 'vnd.openxmlformats-officedocument.wordprocessingml.document'),
        'doc': ('application', 'msword'),
        'xlsx': ('application', 'vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
        'xls': ('application', 'vnd.ms-excel'),
        'csv': ('text', 'csv'),
        'txt': ('text', 'plain'),
        'png': ('image', 'png'),
        'jpg': ('image', 'jpeg'),
        'jpeg': ('image', 'jpeg'),
        'zip': ('application', 'zip'),
    }.get(ext, ('application', 'octet-stream'))


def _send_via_brevo(to_email, subject, body, attachments, reply_to):
    """Send through Brevo's transactional email HTTP API (port 443).

    `attachments` = list of (bytes, filename).
    """
    api_key = os.getenv('BREVO_API_KEY')
    # Sender MUST be an address you verified in Brevo (Senders & IP page).
    sender_email = os.getenv('BREVO_SENDER', os.getenv('SMTP_USER'))
    sender_name = os.getenv('BREVO_SENDER_NAME', 'DigiRocket')
    if not sender_email:
        raise RuntimeError("BREVO_SENDER (or SMTP_USER) not set - need a verified sender email")

    payload = {
        'sender': {'email': sender_email, 'name': sender_name},
        'to': [{'email': to_email}],
        'subject': subject or 'Your Report',
        'textContent': body or _DEFAULT_BODY,
    }
    if attachments:
        payload['attachment'] = [{'content': base64.b64encode(b).decode('ascii'), 'name': n} for (b, n) in attachments]
    if reply_to:
        payload['replyTo'] = {'email': reply_to}

    resp = requests.post(
        'https://api.brevo.com/v3/smtp/email',
        headers={'api-key': api_key, 'content-type': 'application/json', 'accept': 'application/json'},
        data=json.dumps(payload),
        timeout=90,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Brevo send failed ({resp.status_code}): {resp.text}")
    return True


def _send_via_smtp(to_email, subject, body, attachments, reply_to):
    """Send through SMTP (works locally; blocked on Render free tier).

    `attachments` = list of (bytes, filename).
    """
    host = os.getenv('SMTP_HOST', 'smtp.gmail.com')
    port = int(os.getenv('SMTP_PORT', '587'))
    user = os.getenv('SMTP_USER')
    password = os.getenv('SMTP_PASSWORD')
    if not user or not password:
        raise RuntimeError("SMTP_USER / SMTP_PASSWORD not set in .env")

    msg = EmailMessage()
    msg['Subject'] = subject or 'Your Report'
    msg['From'] = user
    msg['To'] = to_email
    # Fresh Message-ID each time -> a brand-new email, never threaded as a reply
    msg['Message-ID'] = make_msgid()
    if reply_to:
        msg['Reply-To'] = reply_to
    msg.set_content(body or _DEFAULT_BODY)
    for b, n in attachments:
        maintype, subtype = _guess_mime(n)
        msg.add_attachment(b, maintype=maintype, subtype=subtype, filename=n)

    with smtplib.SMTP(host, port, timeout=90) as server:
        server.starttls()
        server.login(user, password)
        server.send_message(msg)
    return True


def send_email_with_attachments(to_email, subject, body, attachments, reply_to=None):
    """Email one or more files to `to_email`.

    `attachments` = list of (bytes, filename). Uses Brevo's HTTP API if
    BREVO_API_KEY is set (required on Render), else SMTP (local dev).
    """
    if not to_email:
        raise ValueError("Recipient (client) email is required")
    attachments = [(b, n) for (b, n) in (attachments or []) if b]
    if os.getenv('BREVO_API_KEY'):
        return _send_via_brevo(to_email, subject, body, attachments, reply_to)
    return _send_via_smtp(to_email, subject, body, attachments, reply_to)


def send_report_email(to_email, subject, body, pdf_path, reply_to=None, attachment_name='analytics_report.pdf'):
    """Send the generated PDF report file at `pdf_path` to `to_email`."""
    if not os.path.exists(pdf_path):
        raise FileNotFoundError("Report PDF not found - generate a report first")
    with open(pdf_path, 'rb') as f:
        data = f.read()
    return send_email_with_attachments(to_email, subject, body, [(data, attachment_name)], reply_to)
