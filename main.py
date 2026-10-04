import os
import asyncio
import logging
import sqlite3
import requests
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# ================= Configuration (Render Variables မှ တိုက်ရိုက်ဖတ်ယူခြင်း) =================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
HIGGSFIELD_API_KEY = os.getenv("HIGGSFIELD_API_KEY", "").strip()
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0").strip() or 0)

BASE_URL = "https://api.higgsfield.ai"
INITIAL_FREE_CREDITS = 1

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

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
    headers = {
        "Authorization": f"Bearer {HIGGSFIELD_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "kling-3.0",
        "prompt": prompt,
        "duration": 5
    }

    response = requests.post(f"{BASE_URL}/v1/video/generations", json=payload, headers=headers)
    res_data = response.json()

    if "video_url" in res_data:
        return res_data["video_url"]

    task_id = res_data.get("id") or res_data.get("task_id")
    if not task_id:
        raise Exception(f"API Error: {res_data}")

    for _ in range(60):
        await asyncio.sleep(5)
        status_res = requests.get(f"{BASE_URL}/v1/tasks/{task_id}", headers=headers)
        status_data = status_res.json()

        status = status_data.get("status")
        if status in ["completed", "succeeded"]:
            return status_data.get("output", {}).get("video_url") or status_data.get("video_url")
        elif status == "failed":
            raise Exception("ဗီဒီယို ဖန်တီးမှု မအောင်မြင်ပါ။")

    raise Exception("ဗီဒီယို ဖန်တီးချိန် ကြာမြင့်လွန်းသဖြင့် ရပ်တန့်သွားပါသည်။")

# ================= Bot Handlers =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    credits = get_or_create_user(user_id)
    welcome_text = (
        f"မင်္ဂလာပါ {update.effective_user.first_name}။\n\n"
        f"AI Video Bot မှ ကြိုဆိုပါတယ်။ သင့်တွင် ဗီဒီယိုထုတ်လုပ်နိုင်သော Credit: {credits} ကြိမ် ရှိပါသည်။\n\n"
        "🎬 ဗီဒီယို ထုတ်လုပ်ရန်အတွက် လိုချင်သည့် အင်္ဂလိပ်စာသား Prompt ကို တိုက်ရိုက် ရိုက်ပို့ပေးပါ။"
    )
    await update.message.reply_text(welcome_text)

async def check_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    credits = get_or_create_user(user_id)
    await update.message.reply_text(f"📊 သင်၏ လက်ကျန် Video Generation Credit: {credits} ကြိမ် ဖြစ်ပါသည်။")

async def add_user_credit_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return

    try:
        target_user = int(context.args[0])
        amount = int(context.args[1])
        add_credits(target_user, amount)
        await update.message.reply_text(f"✅ User ID {target_user} သို့ Credit {amount} ကြိမ် ဖြည့်သွင်းပြီးပါပြီ။")
        await context.bot.send_message(chat_id=target_user, text=f"🎉 သင့်ထံသို့ Video Credit {amount} ကြိမ် ထပ်မံဖြည့်သွင်းပေးထားပါပြီ။")
    except Exception:
        await update.message.reply_text("အသုံးပြုပုံ: `/addcredit <user_id> <amount>`")

async def handle_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    prompt_text = update.message.text

    current_credits = get_or_create_user(user_id)
    if current_credits <= 0:
        await update.message.reply_text(
            "⚠️ သင်၏ Video Generation Credit ကုန်ဆုံးသွားပါပြီ။\n\n"
            "Credit ထပ်မံဝယ်ယူရန် ဆက်သွယ်ပေးပါခင်ဗျာ။"
        )
        return

    status_msg = await update.message.reply_text(
        f"🎬 Prompt: '{prompt_text}'\n\nဗီဒီယို စတင်ဖန်တီးနေပါပြီ။ ခန့်မှန်းခြေ ၁ မိနစ်ခန့် ကြာမြင့်ပါမည်..."
    )
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

    try:
        video_url = await generate_video_from_higgsfield(prompt_text)
        if video_url:
            deduct_credit(user_id)
            remaining = current_credits - 1
            await context.bot.send_video(
                chat_id=chat_id,
                video=video_url,
                caption=f"✅ ဗီဒီယို ဖန်တီးပြီးပါပြီ။\nလက်ကျန် Credit: {remaining} ကြိမ်"
            )
            await status_msg.delete()
        else:
            await status_msg.edit_text("❌ ဗီဒီယို ဖိုင် ထုတ်ယူ၍ မရနိုင်ပါ။")
    except Exception as e:
        await status_msg.edit_text(f"⚠️ ချို့ယွင်းချက်: {str(e)}")

def main():
    if not TELEGRAM_BOT_TOKEN:
        print("❌ Error: TELEGRAM_BOT_TOKEN မရှိသေးပါ။ Environment Variables တွင် ထည့်သွင်းပေးပါ။")
        return

    init_db()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("balance", check_balance))
    app.add_handler(CommandHandler("addcredit", add_user_credit_admin))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_prompt))

    print("🚀 Business Video Bot စတင် အလုပ်လုပ်နေပါပြီ...")
    app.run_polling()

if __name__ == "__main__":
    main()
