"""Sending email: console (local testing), SMTP (any provider), or Brevo's HTTPS API.

Free hosts block SMTP ports, which is why Brevo (HTTPS, 300 emails/day free)
is the recommended provider when hosting for free.
"""
from __future__ import annotations

import html
import json
import logging
import smtplib
import ssl
import uuid
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

import httpx

from ..config import ROOT, settings

log = logging.getLogger(__name__)
OUTBOX = ROOT / "data" / "outbox"  # console provider writes every email here


@dataclass
class Mail:
    to: str
    subject: str
    text: str
    html: str
    from_email: str = ""
    from_name: str = ""
    reply_to: str = ""
    headers: dict = field(default_factory=dict)
    tags: list = field(default_factory=list)


class SendError(Exception):
    def __init__(self, msg: str, permanent: bool = False):
        super().__init__(msg)
        self.permanent = permanent  # True = the address is bad (counts as a bounce)


def provider_name() -> str:
    p = settings.mail_provider
    if p == "brevo" and not settings.brevo_api_key:
        return "console"
    if p == "smtp" and not settings.smtp_host:
        return "console"
    return p if p in ("console", "smtp", "brevo") else "console"


def send(mail: Mail) -> str:
    """Send one email and return its Message-ID. Raises SendError."""
    mail.from_email = mail.from_email or settings.mail_from
    mail.from_name = mail.from_name or settings.mail_from_name
    p = provider_name()
    if p == "brevo":
        return _send_brevo(mail)
    if p == "smtp":
        return _send_smtp(mail)
    return _send_console(mail)


def _send_console(mail: Mail) -> str:
    OUTBOX.mkdir(parents=True, exist_ok=True)
    mid = f"<{uuid.uuid4().hex}@loopx.local>"
    rec = {"message_id": mid, "to": mail.to, "from": formataddr((mail.from_name, mail.from_email)),
           "reply_to": mail.reply_to, "subject": mail.subject, "headers": mail.headers, "text": mail.text}
    with open(OUTBOX / "outbox.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")
    log.info("[console mail] to=%s subject=%s", mail.to, mail.subject)
    return mid


def _send_smtp(mail: Mail) -> str:
    msg = EmailMessage()
    domain = mail.from_email.split("@")[-1] or "loopx.local"
    mid = make_msgid(domain=domain)
    msg["Message-ID"] = mid
    msg["From"] = formataddr((mail.from_name, mail.from_email))
    msg["To"] = mail.to
    msg["Subject"] = mail.subject
    if mail.reply_to:
        msg["Reply-To"] = mail.reply_to
    for k, v in mail.headers.items():
        msg[k] = v
    msg.set_content(mail.text)
    msg.add_alternative(mail.html, subtype="html")
    try:
        if settings.smtp_ssl:
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=ssl.create_default_context(), timeout=30) as s:
                _login_send(s, msg)
        else:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as s:
                s.starttls(context=ssl.create_default_context())
                _login_send(s, msg)
    except smtplib.SMTPRecipientsRefused as e:
        raise SendError(f"Recipient refused: {e}", permanent=True)
    except (smtplib.SMTPException, OSError) as e:
        raise SendError(f"SMTP error: {e}")
    return mid


def _login_send(s, msg):
    if settings.smtp_user:
        s.login(settings.smtp_user, settings.smtp_password)
    s.send_message(msg)


def _send_brevo(mail: Mail) -> str:
    body = {
        "sender": {"email": mail.from_email, "name": mail.from_name},
        "to": [{"email": mail.to}],
        "subject": mail.subject,
        "htmlContent": mail.html,
        "textContent": mail.text,
        "headers": mail.headers,
    }
    if mail.reply_to:
        body["replyTo"] = {"email": mail.reply_to}
    if mail.tags:
        body["tags"] = mail.tags[:10]
    try:
        r = httpx.post("https://api.brevo.com/v3/smtp/email", json=body, timeout=30,
                       headers={"api-key": settings.brevo_api_key, "accept": "application/json"})
    except httpx.HTTPError as e:
        raise SendError(f"Brevo unreachable: {e}")
    if r.status_code >= 400:
        permanent = r.status_code == 400 and "email" in r.text.lower()
        raise SendError(f"Brevo {r.status_code}: {r.text[:300]}", permanent=permanent)
    return r.json().get("messageId", "")


# ---------- rendering ----------
def render_html(paragraphs: list[str], cta: str | None, cta_url: str | None, footer: str, unsub_url: str,
                pixel_url: str | None, company: str) -> str:
    esc = html.escape
    ps = "".join(f'<p style="margin:0 0 14px;line-height:1.55">{esc(p).replace(chr(10), "<br>")}</p>' for p in paragraphs)
    btn = (f'<p style="margin:22px 0"><a href="{esc(cta_url)}" style="background:#5B3DF5;color:#fff;'
           f'padding:11px 20px;border-radius:8px;text-decoration:none;display:inline-block">{esc(cta)}</a></p>'
           if cta and cta_url else "")
    px = f'<img src="{esc(pixel_url)}" width="1" height="1" alt="" style="display:block;border:0">' if pixel_url else ""
    return (f'<!doctype html><html><body style="margin:0;background:#F6F5FB">'
            f'<div style="max-width:560px;margin:0 auto;padding:28px 24px;font:15px/1.5 Arial,Helvetica,sans-serif;color:#1E1638">'
            f'<div style="font-weight:700;font-size:17px;margin-bottom:18px">{esc(company)}</div>{ps}{btn}'
            f'<hr style="border:0;border-top:1px solid #E4E1F0;margin:26px 0 12px">'
            f'<p style="font-size:12px;color:#6B6585;margin:0">{esc(footer)} '
            f'<a href="{esc(unsub_url)}" style="color:#6B6585">Unsubscribe</a></p>{px}</div></body></html>')


def read_outbox() -> list[dict]:
    p = OUTBOX / "outbox.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
