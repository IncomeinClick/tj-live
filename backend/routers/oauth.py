"""Restream OAuth flow"""
import json
import secrets
import urllib.parse
import urllib.request
from pathlib import Path

from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from backend.database import get_db
from backend.models import Setting
from backend.auth import require_auth


def get_env(key: str, default: str = "") -> str:
    env = {}
    for line in (Path(__file__).parent.parent.parent / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env.get(key, default)


RESTREAM_CLIENT_ID = get_env("RESTREAM_CLIENT_ID")
RESTREAM_CLIENT_SECRET = get_env("RESTREAM_CLIENT_SECRET")
RESTREAM_REDIRECT_URI = get_env("RESTREAM_REDIRECT_URI")

RESTREAM_AUTH_URL = "https://api.restream.io/login"
RESTREAM_TOKEN_URL = "https://api.restream.io/oauth/token"

router = APIRouter(prefix="/oauth")


@router.get("/connect")
async def connect_restream(request: Request):
    """Redirect to Restream authorization page. Auth via ?token= since browser redirect can't send headers."""
    token = request.query_params.get("token", "")
    from backend.auth import get_auth_config
    _, password = get_auth_config()
    if token != password:
        raise HTTPException(401, "Unauthorized")

    state = secrets.token_urlsafe(16)
    scopes = "profile.default.read channel.default.read stream.default.read clip.default.read"
    params = {
        "response_type": "code",
        "client_id": RESTREAM_CLIENT_ID,
        "redirect_uri": RESTREAM_REDIRECT_URI,
        "state": state,
        "scope": scopes,
    }
    url = f"{RESTREAM_AUTH_URL}?" + urllib.parse.urlencode(params)
    return RedirectResponse(url)


@router.get("/callback")
async def restream_callback(request: Request, db: AsyncSession = Depends(get_db)):
    """Handle OAuth callback from Restream"""
    code = request.query_params.get("code")
    if not code:
        return HTMLResponse("<h1>Error: No code returned</h1>", status_code=400)

    # Exchange code for access token
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": RESTREAM_REDIRECT_URI,
    }).encode()

    import base64
    auth_header = base64.b64encode(f"{RESTREAM_CLIENT_ID}:{RESTREAM_CLIENT_SECRET}".encode()).decode()

    req = urllib.request.Request(
        RESTREAM_TOKEN_URL,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {auth_header}",
        },
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        token_data = json.loads(resp.read())
    except Exception as e:
        return HTMLResponse(f"<h1>Token exchange failed</h1><pre>{e}</pre>", status_code=500)

    # Save to settings
    for key, setting_key in [
        ("access_token", "restream_access_token"),
        ("refresh_token", "restream_refresh_token"),
        ("expires_in", "restream_expires_in"),
    ]:
        if key in token_data:
            existing = await db.get(Setting, setting_key)
            if existing:
                existing.value = str(token_data[key])
            else:
                db.add(Setting(key=setting_key, value=str(token_data[key])))
    await db.commit()

    return HTMLResponse("""
    <html><head><title>Connected</title>
    <style>body{background:#0a0a0a;color:#fff;font-family:sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
    .box{text-align:center;padding:40px}.check{color:#00e676;font-size:4em}</style></head>
    <body><div class="box">
    <div class="check">✓</div>
    <h1>Restream Connected!</h1>
    <p>You can close this window and return to TJ Live.</p>
    <a href="/" style="color:#00e676">← Back to TJ Live</a>
    </div></body></html>
    """)


@router.get("/status", dependencies=[Depends(require_auth)])
async def restream_status(db: AsyncSession = Depends(get_db)):
    """Check if Restream is connected"""
    token = await db.get(Setting, "restream_access_token")
    return {"connected": bool(token and token.value)}


@router.post("/disconnect", dependencies=[Depends(require_auth)])
async def restream_disconnect(db: AsyncSession = Depends(get_db)):
    """Disconnect Restream"""
    for key in ["restream_access_token", "restream_refresh_token", "restream_expires_in"]:
        s = await db.get(Setting, key)
        if s:
            await db.delete(s)
    await db.commit()
    return {"ok": True}
