import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes


BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env var is required")

CHAT_ID = int(os.environ.get("CHAT_ID", "-1003771996489"))
DATA_FILE = Path(os.environ.get("DATA_FILE", "shifts.json"))

DEFAULT_TZ = os.environ.get("DEFAULT_TZ", "Asia/Manila")

USER_TIMEZONES_BY_ID = {
    # Telegram user_id: timezone
    # 123456789: "Asia/Manila",
    # 987654321: "Asia/Jerusalem",
}

USER_TIMEZONES_BY_USERNAME = {
    "alexshatsky": "Asia/Jerusalem",
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


def get_configured_tz_name(user) -> str:
    if user.id in USER_TIMEZONES_BY_ID:
        return USER_TIMEZONES_BY_ID[user.id]

    username = (user.username or "").lower()
    if username in USER_TIMEZONES_BY_USERNAME:
        return USER_TIMEZONES_BY_USERNAME[username]

    return DEFAULT_TZ


def get_user_tz_name(record: dict, user) -> str:
    if record and record.get("timezone"):
        return record["timezone"]

    return get_configured_tz_name(user)


def get_user_tz(record: dict, user) -> ZoneInfo:
    return ZoneInfo(get_user_tz_name(record, user))


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
            "timezone": get_configured_tz_name(user),
            "active_start": None,
            "last_worked_out": None,
            "shifts": [],
        }

    data[uid]["name"] = user.first_name or ""
    data[uid]["username"] = user.username or ""

    if not data[uid].get("timezone"):
        data[uid]["timezone"] = get_configured_tz_name(user)

    return data[uid]


def is_allowed_chat(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.id == CHAT_ID)


async def shift_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed_chat(update):
        return

    user = update.effective_user
    current_utc = now_utc()

    async with LOCK:
        data = load_data()
        record = get_or_create_user(data, user)
        local_tz = get_user_tz(record, user)
        local_time = current_utc.astimezone(local_tz)

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

    async with LOCK:
        data = load_data()
        record = get_or_create_user(data, user)
        local_tz = get_user_tz(record, user)
        local_time = current_utc.astimezone(local_tz)

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

    async with LOCK:
        data = load_data()
        record = get_or_create_user(data, user)
        local_tz = get_user_tz(record, user)
        local_time = current_utc.astimezone(local_tz)

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


async def set_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed_chat(update):
        return

    user = update.effective_user

    if len(context.args) != 1:
        await update.message.reply_text(
            "Usage: /set_tz Asia/Jerusalem or /set_tz Asia/Manila"
        )
        return

    tz_name = context.args[0].strip()

    try:
        ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        await update.message.reply_text(
            f"⚠️ Unknown timezone: {escape(tz_name)}",
            parse_mode="HTML",
        )
        return

    current_utc = now_utc()
    local_time = current_utc.astimezone(ZoneInfo(tz_name))

    async with LOCK:
        data = load_data()
        record = get_or_create_user(data, user)
        record["timezone"] = tz_name
        save_data(data)

    await update.message.reply_text(
        f"✅ <b>{escape(user_label(user))}</b> timezone set to <b>{escape(tz_name)}</b>\n"
        f"Local time now: <b>{local_time:%Y-%m-%d %H:%M}</b>",
        parse_mode="HTML",
    )


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed_chat(update):
        return

    user = update.effective_user
    current_utc = now_utc()

    async with LOCK:
        data = load_data()
        record = get_or_create_user(data, user)
        local_tz = get_user_tz(record, user)
        local_time = current_utc.astimezone(local_tz)
        save_data(data)

    text = (
        f"👤 <b>{escape(user_label(user))}</b>\n"
        f"Telegram ID: <code>{user.id}</code>\n"
        f"Timezone: <b>{escape(local_tz.key)}</b>\n"
        f"Local time: <b>{local_time:%Y-%m-%d %H:%M}</b>"
    )

    await update.message.reply_text(text, parse_mode="HTML")


def main():
    logging.basicConfig(level=logging.INFO)

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("shift_start", shift_start))
    app.add_handler(CommandHandler("shift_end", shift_end))
    app.add_handler(CommandHandler("worked_out", worked_out))
    app.add_handler(CommandHandler("set_tz", set_timezone))
    app.add_handler(CommandHandler("whoami", whoami))

    logging.info("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
