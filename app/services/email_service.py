"""Email notification service using Python stdlib smtplib (no pip deps).

Env vars:
    SMTP_HOST        — SMTP server (default: smtp.gmail.com)
    SMTP_PORT        — SMTP port (default: 587 for STARTTLS)
    SMTP_USER        — login username (usually your email)
    SMTP_PASSWORD    — login password (Gmail: use App Password, not account password)
    SMTP_FROM        — From address (defaults to SMTP_USER)
    NOTIFY_EMAIL_ENABLED — "1" to enable sending (default: "0")

Usage:
    from app.services.email_service import send_email, send_notification

    # Raw email
    send_email("you@example.com", "Subject", "<h1>Hello</h1>")

    # Quick notification (uses NOTIFY_EMAIL_TO)
    send_notification("New Proposal", "AAPL buy signal triggered at $185")
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

_log = logging.getLogger(__name__)


def _notify_enabled() -> bool:
    return os.getenv("NOTIFY_EMAIL_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}


def _smtp_config() -> dict:
    return {
        "host": os.getenv("SMTP_HOST", "smtp.gmail.com").strip(),
        "port": int(os.getenv("SMTP_PORT", "587").strip()),
        "user": os.getenv("SMTP_USER", "").strip(),
        "password": os.getenv("SMTP_PASSWORD", "").strip(),
        "from_addr": os.getenv("SMTP_FROM", "").strip() or os.getenv("SMTP_USER", "").strip(),
    }


def send_email(to: str, subject: str, body_html: str) -> bool:
    """Send an HTML email. Returns True on success, False on failure."""
    cfg = _smtp_config()
    if not cfg["user"] or not cfg["password"]:
        _log.warning("[email] SMTP_USER or SMTP_PASSWORD not set — skipping send")
        return False
    if not to:
        _log.warning("[email] No recipient address — skipping send")
        return False

    msg = MIMEMultipart("alternative")
    msg["From"] = cfg["from_addr"]
    msg["To"] = to
    msg["Subject"] = subject

    # Plain-text fallback
    import re
    plain = re.sub(r"<[^>]+>", "", body_html)
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(body_html, "html"))

    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=15) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(cfg["user"], cfg["password"])
            server.sendmail(cfg["from_addr"], [to], msg.as_string())
        _log.info("[email] Sent to %s: %s", to, subject)
        return True
    except Exception as exc:
        _log.error("[email] Failed to send to %s: %s", to, exc)
        return False


def _get_user_email() -> str:
    """Load the notification recipient from environment configuration."""
    return os.getenv("NOTIFY_EMAIL_TO", "").strip()


def send_notification(subject: str, body_html: str) -> bool:
    """Send a notification email to the configured recipient.

    Only sends if NOTIFY_EMAIL_ENABLED=1 and NOTIFY_EMAIL_TO is set.
    Safe to call anywhere — silently no-ops if not configured.
    """
    if not _notify_enabled():
        return False
    to = _get_user_email()
    if not to:
        _log.debug("[email] NOTIFY_EMAIL_TO is not set — skipping notification")
        return False
    # Wrap in a styled template
    html = f"""
    <div style="font-family: 'Helvetica Neue', Arial, sans-serif; max-width: 560px; margin: 0 auto; padding: 24px;">
      <div style="font-size: 18px; font-weight: 700; color: #1e293b; margin-bottom: 4px;">Investor OS</div>
      <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 12px 0;">
      <div style="font-size: 15px; color: #334155; line-height: 1.6;">
        {body_html}
      </div>
      <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 16px 0 8px;">
      <div style="font-size: 11px; color: #94a3b8;">This is an automated notification from Investor OS.</div>
    </div>
    """
    return send_email(to, f"[Investor OS] {subject}", html)
