"""TJ Live — Live stream dashboard with auto clip cutter + multi-platform scheduler."""
import hashlib
import logging
import secrets as sec
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.auth import _load_env, _save_env, make_token

logging.basicConfig(level=logging.INFO)

# ── Config ──
APP_DIR = Path(__file__).parent
ENV = _load_env()

PORT = int(ENV.get("PORT", 8800))
HOST = ENV.get("HOST", "127.0.0.1")
STATIC_DIR = APP_DIR / "static"
MEDIA_DIR = Path(ENV.get("MEDIA_DIR") or (APP_DIR / "media"))
MEDIA_DIR.mkdir(exist_ok=True, parents=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from backend.database import init_db
    await init_db()
    from backend.services.scheduler import start_scheduler
    start_scheduler()
    yield
    from backend.services.scheduler import stop_scheduler
    stop_scheduler()


app = FastAPI(title="TJ Live", lifespan=lifespan)

# ── Routers ──
from backend.routers.projects import router as projects_router
from backend.routers.promos import router as promos_router
from backend.routers.oauth import router as oauth_router
app.include_router(projects_router)
app.include_router(promos_router)
app.include_router(oauth_router)


# ── Public config (read by the frontend on load) ──
@app.get("/api/config")
async def public_config():
    env = _load_env()
    return {
        "documentor_url": env.get("DOCUMENTOR_URL", ""),
        "public_url": env.get("PUBLIC_URL", ""),
    }


# ── Setup wizard + login ──
class _AuthBody(BaseModel):
    email: str
    password: str


@app.get("/api/setup-status")
async def setup_status():
    env = _load_env()
    has_login = bool(env.get("USER_EMAIL") and env.get("USER_PASS_HASH") and env.get("SECRET_KEY"))
    has_legacy = bool(env.get("AUTH_EMAIL") and env.get("AUTH_PASSWORD"))
    return {"needs_setup": not (has_login or has_legacy), "has_login": has_login or has_legacy}


@app.post("/api/setup")
async def do_setup(body: _AuthBody):
    env = _load_env()
    if env.get("USER_PASS_HASH") or env.get("AUTH_PASSWORD"):
        raise HTTPException(400, "Already configured. Edit .env to change credentials.")
    if not body.email or not body.password:
        raise HTTPException(400, "Email and password are required.")
    if len(body.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters.")

    env["USER_EMAIL"] = body.email
    env["USER_PASS_HASH"] = hashlib.sha256(body.password.encode()).hexdigest()
    env.setdefault("SECRET_KEY", sec.token_urlsafe(32))
    env.setdefault("PORT", str(PORT))
    env.setdefault("HOST", HOST)
    _save_env(env)

    return JSONResponse({"ok": True, "token": make_token(body.email, env["SECRET_KEY"])})


@app.post("/api/login")
async def login(body: _AuthBody):
    env = _load_env()

    user_email = env.get("USER_EMAIL", "")
    user_pass_hash = env.get("USER_PASS_HASH", "")
    secret = env.get("SECRET_KEY", "")
    if user_email and user_pass_hash and secret:
        pass_hash = hashlib.sha256(body.password.encode()).hexdigest()
        if body.email == user_email and pass_hash == user_pass_hash:
            return JSONResponse({"token": make_token(body.email, secret)})
        raise HTTPException(401, "Invalid credentials")

    # Legacy fallback for installs from before the wizard
    legacy_email = env.get("AUTH_EMAIL", "")
    legacy_pw = env.get("AUTH_PASSWORD", "")
    if legacy_email and legacy_pw and body.email == legacy_email and body.password == legacy_pw:
        return JSONResponse({"token": legacy_pw})

    raise HTTPException(401, "Invalid credentials")


# ── Static pages ──
@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/live/{project_id}", response_class=HTMLResponse)
async def live_mode(project_id: str):
    return FileResponse(STATIC_DIR / "live.html")


# ── Media ──
app.mount("/media", StaticFiles(directory=str(MEDIA_DIR)), name="media")
ASSETS_DIR = APP_DIR / "assets"
ASSETS_DIR.mkdir(exist_ok=True)
app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=HOST, port=PORT, reload=True)
