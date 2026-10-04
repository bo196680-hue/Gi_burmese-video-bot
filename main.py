import os
import asyncio
import logging
import sqlite3
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# ================= Configuration =================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
# Requirement 2: Read credential only from HF_API_KEY
HF_API_KEY = os.getenv("HF_API_KEY", "").strip()

admin_env = os.getenv("ADMIN_USER_ID", "0").strip()
ADMIN_USER_ID = int(admin_env) if admin_env.isdigit() else 0

INITIAL_FREE_CREDITS = 1

# Higgsfield Official Endpoint
HIGGSFIELD_API_BASE = "https://api.higgsfield.ai"

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

# ================= Health Check Server for Render Web Service =================
class SimpleHealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is healthy and running!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHealthCheckHandler)
    server.serve_forever()

# ================= Database Management =================
def init_db():
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            credits INTEGER DEFAULT 1
        )
    """)
    conn.commit()
    conn.close()

def get_or_create_user(user_id: int) -> int:
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT credits FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if row is None:
        cursor.execute("INSERT INTO users (user_id, credits) VALUES (?, ?)", (user_id, INITIAL_FREE_CREDITS))
        conn.commit()
        credits = INITIAL_FREE_CREDITS
    else:
        credits = row[0]
    conn.close()
    return credits

def deduct_credit(user_id: int):
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET credits = credits - 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def add_credits(user_id: int, amount: int):
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO users (user_id, credits) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET credits = credits + ?", (user_id, amount, amount))
    conn.commit()
    conn.close()

# ================= Higgsfield Video Generation =================
async def generate_video_from_higgsfield(prompt: str) -> str:
    if not HF_API_KEY:
        raise Exception("HF_API_KEY is missing in environment variables.")

    # Requirement 1: Authorization: Key <complete API key>
    headers = {
        "Authorization": f"Key {HF_API_KEY}",
        "Content-Type": "application/json"
    }

    # Requirement 4: Official Endpoint & Schema
    payload = {
        "prompt": prompt,
        "aspect_ratio": "16:9",
        "duration": 5
    }

    create_url = f"{HIGGSFIELD_API_BASE}/v1/text-to-video"
    response = requests.post(create_url, json=payload, headers=headers)
    
    if response.status_code != 200 and response.status_code != 201:
        raise Exception(f"Higgsfield API Error ({response.status_code}): {response.text}")

    res_data = response.json()

    # Immediate video return check
    if "video_url" in res_data:
        return res_data["video_url"]

    task_id = res_data.get("id") or res_data.get("task_id")
    if not task_id:
        raise Exception(f"Task ID missing in response: {res_data}")

    # Polling for task status
    poll_url = f"{HIGGSFIELD_API_BASE}/v1/text-to-video/{task_id}"
    for _ in range(60):
        await asyncio.sleep(5)
        status_res = requests.get(poll_url, headers=headers)
        if status_res.status_code != 200:
            continue
            
        status_data = status_res.json()
        status = status_data.get("status", "").lower()

        if status in ["completed", "succeeded", "done"]:
            video_url = status_data.get("video_url") or status_data.get("output", {}).get("video_url")
            if not video_url and "videos" in status_data and len(status_data["videos"]) > 0:
                video_url = status_data["videos"][0].get("url")
            return video_url
        elif status in ["failed", "error"]:
            raise Exception(f"Video generation failed: {status_data.get('error', 'Unknown error')}")

    raise Exception("Video generation timed out.")

# ================= Telegram Bot Handlers =================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    credits = get_or_create_user(user_id)
    text = (
        f"မင်္ဂလာပါ {update.effective_user.first_name}!\n\n"
        f"AI Video Bot မှ ကြိုဆိုပါတယ်။ သင်ဖန်တီးလိုသော ဗီဒီယို Prompt စာသားကို ပေးပို့ပေးပါ။\n\n"
        f"လက်ကျန် Video Credit: {credits} ကြိမ်"
    )
    await update.message.reply_text(text)

async def check_balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    credits = get_or_create_user(user_id)
    await update.message.reply_text(f"သင့်ထံတွင် လက်ကျန် Credit: {credits} ကြိမ် ရှိပါသည်။")

async def add_credit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("ဒီ command ကို အသုံးပြုရန် ခွင့်ပြုချက်မရှိပါ။")
        return

    try:
        target_id = int(context.args[0])
        amount = int(context.args[1])
        add_credits(target_id, amount)
        await update.message.reply_text(f"User {target_id} သို့ Credit {amount} ကြိမ် ထည့်သွင်းပေးပြီးပါပြီ။")
    except Exception:
        await update.message.reply_text("အသုံးပြုပုံ: /addcredit <user_id> <amount>")

async def handle_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    prompt = update.message.text
    credits = get_or_create_user(user_id)

    if credits <= 0:
        await update.message.reply_text("သင့်တွင် Credit မလုံလောက်တော့ပါ။ Credit ထပ်မံဖြည့်သွင်းရန် Admin ထံ ဆက်သွယ်ပါ။")
        return

    status_msg = await update.message.reply_text("ဗီဒီယိုကို AI စတင်ဖန်တီးနေပါပြီ... ခေတ္တစောင့်ဆိုင်းပေးပါ။ (၁ မိနစ်မှ ၂ မိနစ်ခန့် ကြာနိုင်ပါသည်)")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.RECORD_VIDEO)

    try:
        video_url = await generate_video_from_higgsfield(prompt)
        deduct_credit(user_id)
        remaining = get_or_create_user(user_id)

        await update.message.reply_video(
            video=video_url,
            caption=f"ဗီဒီယို ဖန်တီးပြီးပါပြီ!\nလက်ကျန် Credit: {remaining} ကြိမ်"
        )
        await status_msg.delete()
    except Exception as e:
        logging.error(f"Error occurred during video processing: {e}")
        await status_msg.edit_text("ဗီဒီယို ဖန်တီးရာတွင် အဆင်မပြေမှု ဖြစ်ပေါ်ခဲ့ပါသည်။ နောက်မှ ထပ်မံကြိုးစားကြည့်ပါ။")

# ================= Main Function =================
def main():
    init_db()

    # Render Port keep-alive
    threading.Thread(target=run_health_server, daemon=True).start()

    clean_token = TELEGRAM_BOT_TOKEN.strip()
    app = ApplicationBuilder().token(clean_token).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("balance", check_balance_command))
    app.add_handler(CommandHandler("addcredit", add_credit_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_prompt))

    logging.info("🚀 Business Video Bot စတင် အလုပ်လုပ်နေပါပြီ...")

    # Requirement 6 & 7: Prevent Telegram Polling Conflict
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
