"""Send the generated PDF report to a client by email (SMTP)."""
import os
import smtplib
from email.message import EmailMessage


def send_report_email(to_email, subject, body, pdf_path, reply_to=None, attachment_name='analytics_report.pdf'):
    """Send `pdf_path` as an attachment to `to_email` via the SMTP account in .env.

    .env keys: SMTP_HOST (default smtp.gmail.com), SMTP_PORT (default 587),
    SMTP_USER (sender email), SMTP_PASSWORD (app password).
    """
    host = os.getenv('SMTP_HOST', 'smtp.gmail.com')
    port = int(os.getenv('SMTP_PORT', '587'))
    user = os.getenv('SMTP_USER')
    password = os.getenv('SMTP_PASSWORD')

    if not user or not password:
        raise RuntimeError("SMTP_USER / SMTP_PASSWORD not set in .env")
    if not to_email:
        raise ValueError("Recipient (client) email is required")
    if not os.path.exists(pdf_path):
        raise FileNotFoundError("Report PDF not found - generate a report first")

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
