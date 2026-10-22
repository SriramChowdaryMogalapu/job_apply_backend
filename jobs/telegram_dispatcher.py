import os
import asyncio
from typing import List
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes
)
from db import init_db_pool, close_db_pool

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8878654242:AAE0ckAgPkhRFGvFHHvqoimSlr7hEpt-_fo")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "1651329503")

# ----------------- 1. DISPATCH DIGEST TO MOBILE -----------------

async def send_morning_digest(limit: int = 7):
    """Fetches top unnotified preferred jobs from Supabase and sends cards to Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[!] Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID environment variables.")
        return

    pool = await init_db_pool()

    query = """
    SELECT id, title, company, location, fit_score, skills_required, experience_required, url, source
    FROM public.jobs
    WHERE status = 'DISCOVERED' 
      AND notified = FALSE 
      AND category = 'Preferred'
    ORDER BY fit_score DESC
    LIMIT $1;
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, limit)

    if not rows:
        print("[*] No new unnotified preferred jobs found for digest.")
        return

    print(f"[*] Dispatching {len(rows)} top jobs to Telegram...")

    async with httpx.AsyncClient(timeout=15) as client:
        dispatched_ids = []

        for job in rows:
            dispatched_ids.append(job["id"])

            msg_text = (
                f"🎯 *{job['fit_score']}% FIT* | *{job['company']}*\n"
                f"💼 *Role:* {job['title']}\n"
                f"📍 *Location:* {job['location']}\n"
                f"⏳ *Exp:* {job['experience_required']}\n"
                f"🛠 *Skills:* {job['skills_required']}\n"
                f"📡 *Source:* {job['source']}\n\n"
                f"🔗 [Open Job Application Link]({job['url']})"
            )

            # Interactive action buttons
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "📥 Queue to Apply", "callback_data": f"queue:{job['id']}"},
                        {"text": "❌ Dismiss", "callback_data": f"dismiss:{job['id']}"}
                    ]
                ]
            }

            payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg_text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": False,
                "reply_markup": keyboard
            }

            resp = await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json=payload
            )
            if resp.status_code != 200:
                print(f"[!] Telegram send error: {resp.text}")

        # Mark sent jobs so they aren't repeated tomorrow
        async with pool.acquire() as conn:
            await conn.execute("UPDATE public.jobs SET notified = TRUE WHERE id = ANY($1::text[]);", dispatched_ids)

    print("[*] Morning digest dispatched successfully.")


# ----------------- 2. INTERACTIVE BUTTON CALLBACK LISTENER -----------------

async def handle_button_press(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles 'Queue' or 'Dismiss' button taps from your phone."""
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    action, _, job_id = data.partition(":")

    pool = await init_db_pool()

    if action == "queue":
        new_status = "QUEUED"
        status_label = "✅ Queued for Evening Apply Copilot"
    elif action == "dismiss":
        new_status = "IGNORED"
        status_label = "🗑️ Dismissed"
    else:
        return

    # Update database state in Supabase
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE public.jobs SET status = $1::job_status WHERE id = $2;",
            new_status, job_id
        )

    # Edit the message on your phone to reflect the action taken
    original_text = query.message.text or query.message.caption or ""
    updated_text = f"{original_text}\n\n---\n*{status_label}*"

    await query.edit_message_text(
        text=updated_text,
        parse_mode="Markdown"
    )

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Sriram's Job Copilot Bot is active!\n"
        "Morning digests will arrive here at 8:00 AM IST.\n"
        "Use /digest to manually trigger a fresh pull anytime."
    )

async def manual_digest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔎 Pulling latest preferred jobs from Supabase...")
    await send_morning_digest(limit=5)


# ----------------- 3. ENTRYPOINT -----------------

def main():
    import sys

    # If invoked directly with 'send', it sends the digest and exits (ideal for cron)
    if len(sys.argv) > 1 and sys.argv[1] == "--send":
        asyncio.run(send_morning_digest())
        return

    # Default: Run bot daemon to listen for interactive taps and manual /digest triggers
    print("[*] Starting Telegram Bot listener daemon...")
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("digest", manual_digest_command))
    app.add_handler(CallbackQueryHandler(handle_button_press))

    app.run_polling()

if __name__ == "__main__":
    main()