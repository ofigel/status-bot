import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes


BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env var is required")

CHAT_ID = int(os.environ.get("CHAT_ID", "-1003771996489"))
DATA_FILE = Path(os.environ.get("DATA_FILE", "shifts.json"))

DEFAULT_TZ = "Asia/Manila"

USER_TIMEZONES = {
    # Telegram user_id: timezone
    # 123456789: "Asia/Manila",
    # 987654321: "Asia/Jerusalem",
}

LOCK = asyncio.Lock()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def load_data() -> dict:
    if not DATA_FILE.exists():
        return {}

    with DATA_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_data(data: dict) -> None:
    tmp_file = DATA_FILE.with_suffix(DATA_FILE.suffix + ".tmp")

    with tmp_file.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    tmp_file.replace(DATA_FILE)


def get_user_tz(user_id: int) -> ZoneInfo:
    tz_name = USER_TIMEZONES.get(user_id, DEFAULT_TZ)
    return ZoneInfo(tz_name)


def format_duration(minutes: float) -> str:
    total = int(round(minutes))
    h = total // 60
    m = total % 60
    return f"{h}h {m}m"


def user_label(user) -> str:
    if user.username:
        return f"@{user.username}"
    return user.first_name or f"user_{user.id}"


def get_or_create_user(data: dict, user) -> dict:
    uid = str(user.id)

    if uid not in data:
        data[uid] = {
            "name": user.first_name or "",
            "username": user.username or "",
            "timezone": USER_TIMEZONES.get(user.id, DEFAULT_TZ),
            "active_start": None,
            "last_worked_out": None,
            "shifts": [],
        }

    data[uid]["name"] = user.first_name or ""
    data[uid]["username"] = user.username or ""
    data[uid]["timezone"] = USER_TIMEZONES.get(user.id, data[uid].get("timezone", DEFAULT_TZ))

    return data[uid]


def is_allowed_chat(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.id == CHAT_ID)


async def shift_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed_chat(update):
        return

    user = update.effective_user
    uid = str(user.id)

    current_utc = now_utc()
    local_tz = get_user_tz(user.id)
    local_time = current_utc.astimezone(local_tz)

    async with LOCK:
        data = load_data()
        record = get_or_create_user(data, user)

        if record.get("active_start"):
            await update.message.reply_text(
                f"⚠️ <b>{escape(user_label(user))}</b> already has an active shift.",
                parse_mode="HTML",
            )
            return

        record["active_start"] = current_utc.isoformat()
        save_data(data)

    text = (
        f"▶️ <b>{escape(user_label(user))}</b> shift started\n"
        f"Local time: <b>{local_time:%H:%M}</b>"
    )

    await update.message.reply_text(text, parse_mode="HTML")


async def shift_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed_chat(update):
        return

    user = update.effective_user
    current_utc = now_utc()
    local_tz = get_user_tz(user.id)
    local_time = current_utc.astimezone(local_tz)

    async with LOCK:
        data = load_data()
        record = get_or_create_user(data, user)

        active_start = record.get("active_start")

        if not active_start:
            await update.message.reply_text(
                f"⚠️ <b>{escape(user_label(user))}</b> has no active shift.",
                parse_mode="HTML",
            )
            return

        start_dt = datetime.fromisoformat(active_start)
        minutes = (current_utc - start_dt).total_seconds() / 60

        record["shifts"].append({
            "start": active_start,
            "end": current_utc.isoformat(),
            "minutes": round(minutes, 2),
        })

        record["active_start"] = None
        save_data(data)

    text = (
        f"⏹️ <b>{escape(user_label(user))}</b> shift ended\n"
        f"Local time: <b>{local_time:%H:%M}</b>\n"
        f"Worked: <b>{format_duration(minutes)}</b>"
    )

    await update.message.reply_text(text, parse_mode="HTML")


async def worked_out(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed_chat(update):
        return

    user = update.effective_user
    current_utc = now_utc()
    local_tz = get_user_tz(user.id)
    local_time = current_utc.astimezone(local_tz)

    async with LOCK:
        data = load_data()
        record = get_or_create_user(data, user)

        if record.get("active_start"):
            await update.message.reply_text(
                f"⚠️ <b>{escape(user_label(user))}</b> has an active shift. End it first with /shift_end.",
                parse_mode="HTML",
            )
            return

        last_worked_out_raw = record.get("last_worked_out")

        if last_worked_out_raw:
            last_worked_out = datetime.fromisoformat(last_worked_out_raw)
        else:
            last_worked_out = datetime.min.replace(tzinfo=timezone.utc)

        total_minutes = 0

        for shift in record.get("shifts", []):
            shift_end_dt = datetime.fromisoformat(shift["end"])

            if shift_end_dt > last_worked_out:
                total_minutes += shift.get("minutes", 0)

        record["last_worked_out"] = current_utc.isoformat()
        save_data(data)

    text = (
        f"💰 <b>{escape(user_label(user))}</b> worked out\n"
        f"Period total: <b>{format_duration(total_minutes)}</b>\n"
        f"Counted until: <b>{local_time:%Y-%m-%d %H:%M}</b>"
    )

    await update.message.reply_text(text, parse_mode="HTML")


def main():
    logging.basicConfig(level=logging.INFO)

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("shift_start", shift_start))
    app.add_handler(CommandHandler("shift_end", shift_end))
    app.add_handler(CommandHandler("worked_out", worked_out))

    logging.info("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
