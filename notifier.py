"""Gmail SMTP notifier — sends the weekly report link to the configured recipient."""
import logging
import os
import smtplib
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


def send_email(subject: str, body: str, to_addr: str = None) -> None:
    user = os.environ["GMAIL_USER"]
    pwd = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")
    recipient = to_addr or user

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = recipient

    logger.info(f"Sending email to {recipient}")
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(user, pwd)
        server.send_message(msg)
    logger.info("Email sent")
