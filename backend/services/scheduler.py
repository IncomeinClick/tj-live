import logging
import json
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from backend.database import async_session
from backend.models import Promo, ShortVideo

logger = logging.getLogger("tj-live.scheduler")
scheduler = AsyncIOScheduler()

UPLOAD_POST_BASE = "https://api.upload-post.com/api"

APP_DIR = Path(__file__).resolve().parent.parent.parent


def _load_env():
    env = {}
    env_path = APP_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def _upload_post_env():
    env = _load_env()
    return env.get("UPLOAD_POST_API_KEY", ""), env.get("UPLOAD_POST_USER", "")


def _telegram_env():
    env = _load_env()
    return env.get("TG_BOT_TOKEN", ""), env.get("TG_CHAT_ID", "")


def send_telegram(message: str):
    import json
    import urllib.request
    bot_token, chat_id = _telegram_env()
    if not bot_token or not chat_id:
        return
    try:
        data = json.dumps({"chat_id": chat_id, "text": message, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data=data, headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


async def check_scheduled_promos():
    """Check for due promos. scheduled_at is stored in Bangkok time (UTC+7)."""
    async with async_session() as db:
        from datetime import timedelta
        now_bkk = datetime.now(timezone.utc) + timedelta(hours=7)
        now_bkk = now_bkk.replace(tzinfo=None)  # Compare naive
        result = await db.execute(
            select(Promo)
            .where(Promo.status == "scheduled")
            .where(Promo.scheduled_at <= now_bkk)
            .order_by(Promo.scheduled_at)
        )
        due = result.scalars().all()

        if not due:
            return

        logger.info(f"Found {len(due)} scheduled promo(s) due")

        for promo in due:
            logger.info(f"Posting promo: {promo.id} ({promo.label})")
            try:
                from backend.services.poster import post_to_all_channels
                results = await post_to_all_channels(promo)
                promo.status = "posted"
                promo.posted_at = datetime.now(timezone.utc)
                await db.commit()
                logger.info(f"Promo {promo.id} posted successfully ({results})")
                channel_status = " ".join([
                    f"FB:{'✅' if results.get('fb_th') else '❌'}",
                    f"IG:{'✅' if results.get('ig_th') else '❌'}",
                    f"Email:{'✅' if results.get('email_th') else '❌'}",
                ])
                send_telegram(f"📢 Promo posted!\n\n📌 {promo.label}\n{channel_status}\n📝 {(promo.caption_th or '')[:100]}...")
            except Exception as e:
                logger.error(f"Promo {promo.id} failed: {e}")
                promo.status = "failed"
                await db.commit()
                send_telegram(f"❌ Promo failed: {promo.label}\n{str(e)[:100]}")


async def check_posted_clips():
    """Detect when upload-post.com has actually published a scheduled clip and notify Telegram.

    ShortVideo.scheduled_at is stored as naive UTC. We give upload-post a 5-minute grace window
    after the scheduled time before checking history, since upload happens around (slightly
    after) the scheduled minute.
    """
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now_utc - timedelta(minutes=3)
    async with async_session() as db:
        result = await db.execute(
            select(ShortVideo)
            .where(ShortVideo.status == "scheduled")
            .where(ShortVideo.scheduled_at != None)  # noqa: E711
            .where(ShortVideo.scheduled_at <= cutoff)
            .where(ShortVideo.upload_post_job_id != None)  # noqa: E711
        )
        due = result.scalars().all()
        if not due:
            return

        api_key, _user = _upload_post_env()
        if not api_key:
            logger.warning("UPLOAD_POST_API_KEY missing; skip clip post check")
            return

        try:
            req = urllib.request.Request(
                f"{UPLOAD_POST_BASE}/uploadposts/history",
                headers={"Authorization": f"Apikey {api_key}"},
            )
            history = json.loads(urllib.request.urlopen(req, timeout=20).read()).get("history", [])
        except Exception as e:
            logger.warning(f"upload-post history fetch failed: {e}")
            return

        # Group history rows by job_id
        by_job: dict[str, list] = {}
        for h in history:
            jid = h.get("job_id")
            if jid:
                by_job.setdefault(jid, []).append(h)

        for sv in due:
            rows = by_job.get(sv.upload_post_job_id, [])
            if not rows:
                continue  # not yet processed by upload-post
            ok = sorted({h["platform"] for h in rows if h.get("success")})
            failed = sorted({h["platform"] for h in rows if not h.get("success")})
            sv.status = "posted"
            sv.posted_at = datetime.now(timezone.utc)
            await db.commit()
            mark = lambda p: "✅" if p in ok else ("❌" if p in failed else "⏳")
            status_line = f"FB:{mark('facebook')} IG:{mark('instagram')} TT:{mark('tiktok')} YT:{mark('youtube')}"
            # Skip Telegram for clips whose schedule is more than 6 hours ago — those are
            # historical catch-ups where Pond doesn't need a fresh notification.
            recent = (now_utc - sv.scheduled_at) < timedelta(hours=6)
            if recent:
                send_telegram(
                    f"🎬 Clip posted!\n\n📌 {(sv.title or '')[:90]}\n{status_line}"
                )
            logger.info(f"Clip {sv.id} marked posted ({status_line}, telegram={recent})")


def start_scheduler():
    scheduler.add_job(
        check_scheduled_promos,
        "interval",
        seconds=60,
        id="check_promos",
        replace_existing=True,
    )
    scheduler.add_job(
        check_posted_clips,
        "interval",
        seconds=120,
        id="check_clips",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("TJ Live scheduler started — promo every 60s, clips every 120s")


def stop_scheduler():
    scheduler.shutdown(wait=False)
