"""Post promos to FB + IG (TH page only) using Documentor credentials."""
import json
import logging
import sqlite3
import urllib.request
import urllib.parse
import asyncio
from pathlib import Path

GRAPH_API = "https://graph.facebook.com/v20.0"
BREVO_BASE = "https://api.brevo.com/v3"

APP_DIR = Path(__file__).resolve().parent.parent.parent

logger = logging.getLogger(__name__)


def _load_env() -> dict:
    env: dict[str, str] = {}
    env_path = APP_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def _get_th_credential():
    """Look up the FB page credential to use for posting.

    Tries Documentor's credentials table first (if DOCUMENTOR_DB + FB_CRED_ID are set),
    else falls back to FB_PAGE_ID + FB_ACCESS_TOKEN from .env directly.
    """
    env = _load_env()
    doc_db = env.get("DOCUMENTOR_DB", "")
    fb_cred_id = env.get("FB_CRED_ID", "")
    if doc_db and fb_cred_id and Path(doc_db).exists():
        conn = sqlite3.connect(doc_db)
        c = conn.cursor()
        c.execute("SELECT page_id, access_token FROM credentials WHERE id=?", (fb_cred_id,))
        row = c.fetchone()
        conn.close()
        if row:
            return {"page_id": row[0], "access_token": row[1]}

    page_id = env.get("FB_PAGE_ID", "")
    access_token = env.get("FB_ACCESS_TOKEN", "")
    if page_id and access_token:
        return {"page_id": page_id, "access_token": access_token}
    return None


async def post_fb_photo(page_id: str, access_token: str, image_path: str, caption: str = "") -> str:
    url = f"{GRAPH_API}/{page_id}/photos"
    boundary = "----TJLiveBoundary"
    body_parts = []
    if caption:
        body_parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"message\"\r\n\r\n{caption}")
    body_parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"access_token\"\r\n\r\n{access_token}")
    with open(image_path, "rb") as f:
        img_data = f.read()
    body_bytes = ("\r\n".join(body_parts) + "\r\n").encode()
    body_bytes += f"--{boundary}\r\nContent-Disposition: form-data; name=\"source\"; filename=\"promo.png\"\r\nContent-Type: image/png\r\n\r\n".encode()
    body_bytes += img_data
    body_bytes += f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(url, data=body_bytes)
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    resp = urllib.request.urlopen(req, timeout=60)
    data = json.loads(resp.read())
    return data.get("post_id") or data.get("id")


async def post_ig_photo(page_id: str, access_token: str, image_url: str, caption: str = "") -> str:
    # Get IG account
    url = f"{GRAPH_API}/{page_id}?fields=instagram_business_account&access_token={access_token}"
    resp = urllib.request.urlopen(url, timeout=30)
    data = json.loads(resp.read())
    ig_id = data.get("instagram_business_account", {}).get("id")
    if not ig_id:
        raise Exception("No IG account linked")

    # Create container
    params = {"image_url": image_url, "access_token": access_token}
    if caption:
        params["caption"] = caption
    create_url = f"{GRAPH_API}/{ig_id}/media?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(create_url, method="POST")
    resp = urllib.request.urlopen(req, timeout=60)
    container_id = json.loads(resp.read()).get("id")

    # Wait for processing
    for _ in range(15):
        await asyncio.sleep(3)
        status_url = f"{GRAPH_API}/{container_id}?fields=status_code&access_token={access_token}"
        resp = urllib.request.urlopen(status_url, timeout=10)
        if json.loads(resp.read()).get("status_code") == "FINISHED":
            break

    # Publish
    pub_url = f"{GRAPH_API}/{ig_id}/media_publish?creation_id={container_id}&access_token={access_token}"
    req = urllib.request.Request(pub_url, method="POST")
    resp = urllib.request.urlopen(req, timeout=60)
    return json.loads(resp.read()).get("id")


def _get_brevo_key() -> str:
    env = _load_env()
    key = env.get("BREVO_API_KEY", "")
    if key:
        return key
    # Fallback: read from a separately-installed Brevo service if BREVO_ENV_PATH is set
    brevo_env_path = env.get("BREVO_ENV_PATH", "")
    if brevo_env_path and Path(brevo_env_path).exists():
        for line in Path(brevo_env_path).read_text().splitlines():
            line = line.strip()
            if line.startswith("BREVO_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _brevo_config() -> dict:
    env = _load_env()
    list_ids = [
        int(x.strip())
        for x in (env.get("BREVO_LIST_IDS", "") or "").split(",")
        if x.strip().isdigit()
    ]
    sender_name = env.get("BREVO_SENDER_NAME", "")
    sender_email = env.get("BREVO_SENDER_EMAIL", "")
    return {
        "list_ids": list_ids,
        "sender": {"name": sender_name, "email": sender_email} if sender_email else None,
    }


def _email_subject(caption: str, label: str) -> str:
    first = (caption or "").strip().split("\n", 1)[0].strip()
    sender_name = _brevo_config()["sender"]["name"] if _brevo_config()["sender"] else "TJ Live"
    subject = first or label or f"อัปเดตจาก {sender_name}"
    return subject[:90]


def _email_html(caption: str, image_url: str) -> str:
    body = (caption or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    sender_name = _brevo_config()["sender"]["name"] if _brevo_config()["sender"] else "TJ Live"
    return (
        "<html><body style=\"font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;color:#222\">"
        f"<img src=\"{image_url}\" alt=\"\" style=\"width:100%;border-radius:8px;display:block\"/>"
        f"<div style=\"white-space:pre-wrap;line-height:1.7;margin-top:20px;font-size:15px\">{body}</div>"
        "<hr style=\"border:none;border-top:1px solid #eee;margin:24px 0\"/>"
        f"<div style=\"font-size:12px;color:#888\">{sender_name} &middot; "
        "<a href=\"{{ unsubscribe }}\" style=\"color:#888\">ยกเลิกรับอีเมล</a></div>"
        "</body></html>"
    )


async def send_brevo_campaign(promo, image_url: str) -> str:
    """Create a Brevo campaign for TH lists and send immediately. Returns campaign id."""
    key = _get_brevo_key()
    if not key:
        raise Exception("BREVO_API_KEY not found")
    cfg = _brevo_config()
    if not cfg["sender"] or not cfg["list_ids"]:
        raise Exception("BREVO_SENDER_EMAIL and BREVO_LIST_IDS must be set in .env to send campaigns")

    name = f"TJ Live promo {promo.id}"[:50]
    payload = {
        "name": name,
        "subject": _email_subject(promo.caption_th or "", promo.label or ""),
        "sender": cfg["sender"],
        "type": "classic",
        "htmlContent": _email_html(promo.caption_th or "", image_url),
        "recipients": {"listIds": cfg["list_ids"]},
        "inlineImageActivation": False,
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(f"{BREVO_BASE}/emailCampaigns", data=data, method="POST")
    req.add_header("api-key", key)
    req.add_header("Content-Type", "application/json")
    req.add_header("accept", "application/json")
    resp = urllib.request.urlopen(req, timeout=30)
    campaign_id = json.loads(resp.read()).get("id")
    if not campaign_id:
        raise Exception("No campaign id returned")

    # Send now
    send_req = urllib.request.Request(
        f"{BREVO_BASE}/emailCampaigns/{campaign_id}/sendNow", data=b"", method="POST"
    )
    send_req.add_header("api-key", key)
    send_req.add_header("accept", "application/json")
    urllib.request.urlopen(send_req, timeout=30)
    return str(campaign_id)


async def post_to_all_channels(promo) -> dict:
    """Post promo to FB TH + IG TH + Brevo email (3 TH lists)."""
    th = _get_th_credential()
    if not th:
        return {"error": "No TH credential found"}

    results = {}

    if not promo.image_path:
        return {"error": "No image"}

    import os
    filename = os.path.basename(promo.image_path)
    public_url = _load_env().get("PUBLIC_URL", "").rstrip("/")
    if not public_url:
        return {"error": "PUBLIC_URL not set in .env — IG and email need a public image URL"}
    ig_image_url = f"{public_url}/media/{filename}"

    # FB TH
    try:
        results["fb_th"] = await post_fb_photo(th["page_id"], th["access_token"], promo.image_path, promo.caption_th or "")
        promo.fb_post_id_th = results["fb_th"]
    except Exception as e:
        results["fb_th_error"] = str(e)

    # IG TH
    try:
        results["ig_th"] = await post_ig_photo(th["page_id"], th["access_token"], ig_image_url, promo.caption_th or "")
        promo.ig_post_id_th = results["ig_th"]
    except Exception as e:
        results["ig_th_error"] = str(e)

    # Email via Brevo (optional — only attempted if Brevo is configured)
    if _get_brevo_key():
        try:
            results["email_th"] = await send_brevo_campaign(promo, ig_image_url)
        except Exception as e:
            results["email_th_error"] = str(e)
            logger.error(f"Brevo campaign failed for promo {promo.id}: {e}")

    return results
