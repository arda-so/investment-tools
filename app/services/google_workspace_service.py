from __future__ import annotations

import datetime as dt
import json
import secrets
import tempfile
import base64
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from app.core.config import ROOT, app_env


SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

TOKEN_PATH = ROOT / "data" / "google_token.json"
STATE_PATH = ROOT / "data" / "google_oauth_state.json"
CLIENT_SECRET_PATH = Path(app_env("GOOGLE_OAUTH_CLIENT_SECRET", str(ROOT / "data" / "google_client_secret.json")))
REDIRECT_URI = app_env("GOOGLE_OAUTH_REDIRECT_URI", "http://localhost:8766/organizer/google/callback")


def _write_json_atomic(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(obj, ensure_ascii=True, indent=2, sort_keys=True)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=str(path.parent), encoding="utf-8") as tmp:
        tmp.write(payload)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        if not path.exists():
            return {}
        obj = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _deps_ok() -> bool:
    try:
        import googleapiclient.discovery  # noqa: F401
        import google.oauth2.credentials  # noqa: F401
        import google_auth_oauthlib.flow  # noqa: F401
    except Exception:
        return False
    return True


def _credentials():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    data = _load_json(TOKEN_PATH)
    if not data:
        return None
    try:
        creds = Credentials.from_authorized_user_info(data, SCOPES)
    except Exception:
        return None
    try:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _write_json_atomic(TOKEN_PATH, json.loads(creds.to_json()))
    except Exception:
        return None
    return creds if creds.valid else None


def google_status() -> dict[str, str]:
    if not _deps_ok():
        return {"connected": "0", "message": "Google dependencies missing: pip install google-auth-oauthlib."}
    if not CLIENT_SECRET_PATH.exists():
        return {
            "connected": "0",
            "message": (
                f"Missing client secret: {CLIENT_SECRET_PATH}. "
                f"OAuth redirect must be: {REDIRECT_URI}"
            ),
        }
    creds = _credentials()
    if creds is None:
        return {"connected": "0", "message": "Not connected."}
    expiry = getattr(creds, "expiry", None)
    expiry_s = expiry.isoformat() if expiry else ""
    return {"connected": "1", "message": "Connected.", "expires_at": expiry_s}


def build_connect_url() -> tuple[str, str]:
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_secrets_file(str(CLIENT_SECRET_PATH), scopes=SCOPES)
    flow.redirect_uri = REDIRECT_URI
    state = secrets.token_urlsafe(24)
    auth_url, _state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state,
    )
    _write_json_atomic(
        STATE_PATH,
        {
            "state": state,
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "redirect_uri": REDIRECT_URI,
        },
    )
    return auth_url, state


def complete_connect(code: str, state: str) -> tuple[bool, str]:
    if not _deps_ok():
        return False, "Google dependencies missing."
    saved = _load_json(STATE_PATH)
    expected_state = str(saved.get("state") or "")
    if not expected_state or not state or state != expected_state:
        return False, "Invalid OAuth state. Retry connect."
    try:
        from google_auth_oauthlib.flow import Flow

        flow = Flow.from_client_secrets_file(str(CLIENT_SECRET_PATH), scopes=SCOPES, state=state)
        flow.redirect_uri = REDIRECT_URI
        flow.fetch_token(code=code)
        creds = flow.credentials
        _write_json_atomic(TOKEN_PATH, json.loads(creds.to_json()))
        try:
            STATE_PATH.unlink(missing_ok=True)
        except Exception:
            pass
        return True, "Google account connected."
    except Exception as exc:
        return False, f"Google OAuth failed: {type(exc).__name__}"


def disconnect_google() -> None:
    try:
        TOKEN_PATH.unlink(missing_ok=True)
    except Exception:
        pass
    try:
        STATE_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def get_today_calendar_events(limit: int = 20) -> list[dict[str, str]]:
    creds = _credentials()
    if creds is None:
        return []
    from googleapiclient.discovery import build

    now = dt.datetime.now().astimezone()
    start = dt.datetime.combine(now.date(), dt.time.min, tzinfo=now.tzinfo)
    end = dt.datetime.combine(now.date(), dt.time.max, tzinfo=now.tzinfo)
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    res = (
        service.events()
        .list(
            calendarId="primary",
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=max(1, min(50, int(limit))),
        )
        .execute()
    )
    out: list[dict[str, str]] = []
    for it in res.get("items", []) or []:
        start_obj = (it.get("start") or {})
        raw_time = str(start_obj.get("dateTime") or start_obj.get("date") or "")
        title = str(it.get("summary") or "(No title)").strip()
        time_label = "All day"
        if "T" in raw_time:
            try:
                t = dt.datetime.fromisoformat(raw_time.replace("Z", "+00:00")).astimezone(now.tzinfo)
                time_label = t.strftime("%H:%M")
            except Exception:
                time_label = raw_time[11:16] if len(raw_time) >= 16 else raw_time
        out.append({"time": time_label, "title": title})
    return out


def get_priority_emails(limit: int = 8) -> list[dict[str, str]]:
    creds = _credentials()
    if creds is None:
        return []
    from googleapiclient.discovery import build

    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    lim = max(1, min(20, int(limit)))
    queries = [
        "in:inbox newer_than:7d",
        "category:primary newer_than:14d",
        "newer_than:14d",
    ]
    msg_list = {"messages": []}
    for q in queries:
        try:
            msg_list = (
                service.users()
                .messages()
                .list(
                    userId="me",
                    maxResults=lim,
                    q=q,
                )
                .execute()
            )
        except Exception:
            msg_list = {"messages": []}
        if (msg_list.get("messages") or []):
            break
    out: list[dict[str, str]] = []
    for m in msg_list.get("messages", []) or []:
        mid = str(m.get("id") or "")
        if not mid:
            continue
        raw = (
            service.users()
            .messages()
            .get(userId="me", id=mid, format="metadata", metadataHeaders=["From", "Subject", "Date"])
            .execute()
        )
        headers = {str(h.get("name") or "").lower(): str(h.get("value") or "") for h in (raw.get("payload") or {}).get("headers", [])}
        out.append(
            {
                "from": headers.get("from", "Unknown"),
                "subject": headers.get("subject", "(No subject)"),
                "date": headers.get("date", ""),
                "snippet": str(raw.get("snippet") or "").strip(),
            }
        )
    return out


def send_email(to_email: str, subject: str, body: str) -> tuple[bool, str]:
    creds = _credentials()
    if creds is None:
        return False, "Google is not connected."
    to_s = str(to_email or "").strip()
    subj_s = str(subject or "").strip()
    body_s = str(body or "").strip()
    if not to_s:
        return False, "Recipient email is required."
    if not subj_s:
        return False, "Subject is required."
    if not body_s:
        return False, "Body is required."
    try:
        from googleapiclient.discovery import build

        msg = EmailMessage()
        msg["To"] = to_s
        msg["Subject"] = subj_s
        msg.set_content(body_s)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return True, "Email sent."
    except Exception as exc:
        return False, f"Send failed: {type(exc).__name__}"
