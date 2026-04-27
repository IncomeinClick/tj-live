"""Setup wizard + JWT-style login for TJ Live.

Fresh installs have an empty .env. The frontend calls /api/setup-status; if
needs_setup is true, it shows a wizard that prompts for an email + password.
The wizard hashes the password (SHA-256), generates a strong SECRET_KEY,
writes .env, and returns a token. Subsequent visits show a normal login form
that exchanges email + password for the token.

If a legacy .env has AUTH_PASSWORD (plain) but no USER_PASS_HASH (older TJ
Live versions used the password itself as the bearer token), require_auth
falls back to comparing against AUTH_PASSWORD so existing installs keep
working without immediate reconfiguration.
"""
import hashlib
import hmac
import json
import secrets as sec
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from pathlib import Path

from fastapi import Request, HTTPException

APP_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = APP_DIR / ".env"
TOKEN_TTL = 30 * 24 * 3600  # 30 days


def _load_env() -> dict:
    env: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def _save_env(env: dict) -> None:
    preferred = [
        "PORT", "HOST", "PUBLIC_URL",
        "USER_EMAIL", "USER_PASS_HASH", "SECRET_KEY",
        "AUTH_EMAIL", "AUTH_PASSWORD",  # legacy — kept if present
        "TG_BOT_TOKEN", "TG_CHAT_ID",
        "UPLOAD_POST_API_KEY", "UPLOAD_POST_USER",
        "RESTREAM_CLIENT_ID", "RESTREAM_CLIENT_SECRET", "RESTREAM_REDIRECT_URI",
        "FB_PAGE_ID", "FB_ACCESS_TOKEN", "FB_CRED_ID",
        "BREVO_API_KEY", "BREVO_LIST_IDS", "BREVO_SENDER_NAME", "BREVO_SENDER_EMAIL",
        "DOCUMENTOR_DB", "DOCUMENTOR_API", "DOCUMENTOR_AUTH", "DOCUMENTOR_TH_PROJECT",
        "MEDIA_DIR", "CLAUDE_CLI", "AUTO_EDITOR", "YTDLP_BIN", "PROMO_SKILL_PATH",
    ]
    lines = ["# TJ Live configuration — managed by the setup wizard."]
    written: set[str] = set()
    for k in preferred:
        if k in env:
            lines.append(f"{k}={env[k]}")
            written.add(k)
    for k, v in env.items():
        if k not in written:
            lines.append(f"{k}={v}")
    ENV_PATH.write_text("\n".join(lines) + "\n")


def make_token(email: str, secret: str) -> str:
    payload = json.dumps({"email": email, "exp": time.time() + TOKEN_TTL})
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return urlsafe_b64encode(payload.encode()).decode() + "." + sig


def verify_signed_token(token: str, secret: str) -> str | None:
    try:
        parts = token.split(".")
        if len(parts) != 2:
            return None
        payload = urlsafe_b64decode(parts[0]).decode()
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(parts[1], expected):
            return None
        data = json.loads(payload)
        if data.get("exp", 0) < time.time():
            return None
        return data.get("email")
    except Exception:
        return None


async def require_auth(request: Request):
    auth = request.headers.get("Authorization", "")
    cookie_token = request.cookies.get("token", "")
    token = ""
    if auth.startswith("Bearer "):
        token = auth[7:]
    elif cookie_token:
        token = cookie_token

    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized")

    env = _load_env()
    secret = env.get("SECRET_KEY", "")
    if secret and verify_signed_token(token, secret):
        return

    # Legacy fallback: token equals plaintext AUTH_PASSWORD (old behaviour)
    legacy_password = env.get("AUTH_PASSWORD", "")
    if legacy_password and hmac.compare_digest(token, legacy_password):
        return

    raise HTTPException(status_code=401, detail="Unauthorized")
