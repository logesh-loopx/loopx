"""Read replies from the reply-to mailbox over IMAP (port 993, open on free hosts)."""
from __future__ import annotations

import email
import imaplib
import logging
import re
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime

from ..config import settings

log = logging.getLogger(__name__)


@dataclass
class InboundMail:
    message_id: str
    from_email: str
    from_name: str
    subject: str
    body: str
    received_at: object  # datetime (naive UTC)
    in_reply_to: str
    to: str
    auto: bool  # auto-submitted / out-of-office headers


def imap_config(ws) -> dict:
    o = (ws.settings or {}).get("imap") or {}
    return {"host": o.get("host") or settings.imap_host, "user": o.get("user") or settings.imap_user,
            "password": o.get("password") or settings.imap_password, "folder": o.get("folder") or settings.imap_folder}


def configured(ws) -> bool:
    c = imap_config(ws)
    return bool(c["host"] and c["user"] and c["password"])


def _dec(v) -> str:
    try:
        return str(make_header(decode_header(v))) if v else ""
    except Exception:
        return str(v or "")


def _body(msg) -> str:
    part = None
    if msg.is_multipart():
        for p in msg.walk():
            if p.get_content_type() == "text/plain" and "attachment" not in str(p.get("Content-Disposition") or ""):
                part = p
                break
        if part is None:
            for p in msg.walk():
                if p.get_content_type() == "text/html":
                    part = p
                    break
    else:
        part = msg
    if part is None:
        return ""
    raw = part.get_payload(decode=True) or b""
    txt = raw.decode(part.get_content_charset() or "utf-8", "replace")
    if part.get_content_type() == "text/html":
        txt = re.sub(r"<[^>]+>", " ", txt)
    return strip_quoted(txt)


def strip_quoted(body: str) -> str:
    keep = []
    for line in body.splitlines():
        s = line.strip()
        if s.startswith(">") or re.match(r"^On .+wrote:$", s) or s.startswith("-----Original Message") or s.startswith("From: "):
            break
        keep.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(keep)).strip()[:4000]


def fetch_unseen(ws, limit: int = 100) -> list[InboundMail]:
    c = imap_config(ws)
    out: list[InboundMail] = []
    with imaplib.IMAP4_SSL(c["host"], timeout=30) as M:
        M.login(c["user"], c["password"])
        M.select(c["folder"])
        _, data = M.search(None, "UNSEEN")
        for num in data[0].split()[-limit:]:
            _, d = M.fetch(num, "(RFC822)")  # marks as seen
            msg = email.message_from_bytes(d[0][1])
            name, addr = parseaddr(_dec(msg.get("From")))
            try:
                dt = parsedate_to_datetime(msg.get("Date"))
                dt = dt.astimezone(tz=__import__("datetime").timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt
            except Exception:
                dt = None
            auto = bool(msg.get("Auto-Submitted", "no").lower() not in ("no", "")) or bool(msg.get("X-Autoreply")) \
                or "auto-reply" in (msg.get("X-Autorespond") or "").lower()
            out.append(InboundMail(message_id=(msg.get("Message-ID") or f"imap-{num.decode()}").strip(),
                                   from_email=addr.lower(), from_name=name, subject=_dec(msg.get("Subject")),
                                   body=_body(msg), received_at=dt, in_reply_to=(msg.get("In-Reply-To") or "").strip(),
                                   to=_dec(msg.get("To")), auto=auto))
    return out
