#!/usr/bin/env python3
"""
Telegram Job Alert Dispatcher
-----------------------------
Pulls newly discovered high-fit jobs from PostgreSQL / Supabase,
formats notification cards, dispatches them to a Telegram chat,
and marks the records as notified.
"""

import os
import sys
import ssl
import socket
import asyncio
import urllib.parse
from typing import List, Dict, Any, Optional

import httpx
import asyncpg


# =====================================================================
# Configuration & Environment Variables
# =====================================================================
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres.faeevhssnyjunbsguqce:%24TxRe%23J_4PG%262%21L@aws-1-ap-northeast-1.pooler.supabase.com:5432/postgres")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8878654242:AAE0ckAgPkhRFGvFHHvqoimSlr7hEpt-_fo")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "1651329503")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://jobapplyfrontend.vercel.app").rstrip("/")

# Number of top job alerts to dispatch per digest run
DIGEST_LIMIT = int(os.getenv("DIGEST_LIMIT", "10"))  # Default to 10 jobs per digest


# =====================================================================
# Database Helpers
# =====================================================================
async def get_db_connection() -> asyncpg.Connection:
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable is missing.")

    clean_uri = (
        DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
        .replace("postgres+asyncpg://", "postgres://")
    )

    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    return await asyncpg.connect(
        clean_uri,
        ssl=ssl_ctx,
        statement_cache_size=0,
    )


async def fetch_pending_jobs(conn: asyncpg.Connection, limit: int = 5) -> List[Dict[str, Any]]:
    """
    Fetches the highest-ranking DISCOVERED jobs that haven't been notified yet.
    Prioritizes 'Preferred' category and highest fit scores.
    """
    query = """
        SELECT 
            id,
            title,
            company,
            location,
            category,
            fit_score,
            skills_required,
            experience_required,
            source,
            url
        FROM public.jobs
        WHERE status = 'DISCOVERED'
        ORDER BY 
            CASE WHEN category = 'Preferred' THEN 1 ELSE 2 END,
            fit_score DESC,
            first_seen_at DESC
        LIMIT $1;
    """
    rows = await conn.fetch(query, limit)
    return [dict(r) for r in rows]


async def mark_jobs_as_notified(conn: asyncpg.Connection, job_ids: List[str]) -> None:
    """
    Updates the database records to avoid sending duplicate alerts.
    """
    if not job_ids:
        return

    query = """
        UPDATE public.jobs
        SET 
            status = 'QUEUED',
            updated_at = NOW()
        WHERE id = ANY($1::text[]);
    """
    await conn.execute(query, job_ids)


# =====================================================================
# Message Formatting & Telegram Dispatcher
# =====================================================================
def format_job_message(job: Dict[str, Any]) -> str:
    """
    Creates a clean, scannable Markdown card for the Telegram chat.
    """
    title = job.get("title", "Unknown Role")
    company = job.get("company", "Unknown Company")
    location = job.get("location", "Remote / Unspecified")
    fit_score = job.get("fit_score", 0)
    source = (job.get("source") or "Job Board").upper()
    exp = job.get("experience_required") or "Not specified"
    skills = job.get("skills_required") or "Not specified"

    if len(skills) > 120:
        skills = skills[:117] + "..."

    return (
        f"🎯 *{title}*\n"
        f"🏢 *Company:* {company}\n"
        f"📍 *Location:* {location}\n"
        f"⚡ *Match Fit:* `{fit_score}%` | *Source:* `{source}`\n"
        f"⏳ *Experience:* {exp}\n"
        f"🛠 *Skills:* {skills}"
    )
    

def create_job_action_keyboard(job_id: str, raw_job_url: Optional[str] = None) -> Dict[str, Any]:
    """
    Builds an interactive Inline Keyboard with:
    - ⚡ Review & AutoFill (opens your Next.js web application)
    - 🌐 View Posting (optional direct link to Greenhouse/Indeed/Lever)
    """
    webapp_url = f"{FRONTEND_URL}/jobs/{job_id}"

    # Option A: Standard URL Button (opens in browser)
    review_button = {
        "text": "⚡ Review & AutoFill",
        "url": webapp_url
    }

    # Option B (Alternative): Web App popup inside Telegram (uncomment if preferred)
    # review_button = {
    #     "text": "⚡ Review & AutoFill",
    #     "web_app": {"url": webapp_url}
    # }

    buttons = [review_button]

    # Optionally add a second button for the raw board link side-by-side
    if raw_job_url:
        buttons.append({
            "text": "🌐 Original Post",
            "url": raw_job_url
        })

    return {
        "inline_keyboard": [buttons]
    }

async def send_telegram_message(
    client: httpx.AsyncClient, 
    bot_token: str, 
    chat_id: str, 
    text: str,
    reply_markup: Optional[Dict[str, Any]] = None
) -> bool:
    """
    Sends a message via the Telegram Bot API with optional inline keyboard buttons.
    """
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,  # Keep cards clean without link previews
    }

    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        response = await client.post(url, json=payload, timeout=15.0)
        if response.status_code == 200:
            return True
        else:
            print(f"[!] Telegram API error ({response.status_code}): {response.text}")
            return False
    except Exception as exc:
        print(f"[!] Network error sending message to Telegram: {exc}")
        return False


# =====================================================================
# Main Orchestration Loop
# =====================================================================
async def dispatch_digest() -> None:
    """
    Main job dispatcher logic.
    """
    print("[*] Starting Telegram notification dispatcher...")

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[!] Error: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing.")
        sys.exit(1)

    # 1. Connect to DB
    print("[*] Connecting to database...")
    try:
        conn = await get_db_connection()
    except Exception as exc:
        print(f"[!] Database connection failed: {exc}")
        sys.exit(1)

    try:
        # 2. Fetch pending jobs
        jobs = await fetch_pending_jobs(conn, limit=DIGEST_LIMIT)
        if not jobs:
            print("[*] No pending DISCOVERED jobs found to dispatch. Exiting.")
            return

        print(f"[*] Found {len(jobs)} jobs to notify.")

        notified_ids = []
        async with httpx.AsyncClient() as http_client:
            # Send intro banner if sending a batch
            header_text = f"📬 *Morning Job Digest: {len(jobs)} New Matched Openings*"
            await send_telegram_message(http_client, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, header_text)
            await asyncio.sleep(0.5)

            # 3. Send each job card
            for index, job in enumerate(jobs, start=1):
                msg = format_job_message(job)
                keyboard = create_job_action_keyboard(job["id"], job.get("url"))
                
                success = await send_telegram_message(
                    client=http_client,
                    bot_token=TELEGRAM_BOT_TOKEN,
                    chat_id=TELEGRAM_CHAT_ID,
                    text=msg,
                    reply_markup=keyboard
                )
                
                if success:
                    notified_ids.append(job["id"])
                    print(f"    [{index}/{len(jobs)}] Sent: {job.get('title')} at {job.get('company')}")
                else:
                    print(f"    [{index}/{len(jobs)}] Failed to send: {job.get('id')}")

                await asyncio.sleep(1.0)

        # 4. Update status in database
        if notified_ids:
            print(f"[*] Updating {len(notified_ids)} jobs to status 'QUEUED'...")
            await mark_jobs_as_notified(conn, notified_ids)
            print("[*] Status update complete.")

    finally:
        await conn.close()
        print("[*] Database connection closed.")


def main():
    try:
        asyncio.run(dispatch_digest())
    except KeyboardInterrupt:
        print("\n[!] Execution stopped by user.")
    except Exception as e:
        print(f"\n[!] Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()