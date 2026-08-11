"""
Habit Tracker — backend + Telegram bot in one process.

Что делает:
- Отдаёт мини-апп (index.html) по адресу "/"
- REST API для мини-аппа: /api/state (GET/POST) — хранит habits + completions по telegram user_id
- Telegram-бот: команда /start открывает мини-апп кнопкой, плюс раз в минуту проверяет
  напоминания (reminderTime у привычек) и шлёт сообщение, если привычка на сегодня не сделана

Запуск:
    pip install -r requirements.txt
    export BOT_TOKEN=твой_токен_от_BotFather
    export WEBAPP_URL=https://твой-домен.up.railway.app
    python main.py
"""

import os
import json
import sqlite3
import asyncio
import logging
import time
from datetime import datetime, date
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager

TZ = ZoneInfo("Europe/Moscow")

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("habit-tracker")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://example.com")
DB_PATH = os.environ.get("DB_PATH", "habits.db")
FALLBACK_DB_PATH = "/app/habits_fallback.db"  # непостоянный, но хотя бы не даёт приложению падать

# ---------------------------------------------------------------- DB

_active_db_path = None

def _prepare_db_path():
    global _active_db_path
    if _active_db_path:
        return _active_db_path

    d = os.path.dirname(DB_PATH) or "."
    try:
        os.makedirs(d, exist_ok=True)
    except Exception as e:
        log.warning("makedirs(%s) failed: %s", d, e)
    try:
        os.chmod(d, 0o777)
    except Exception as e:
        log.warning("chmod(%s) failed: %s", d, e)

    log.info(
        "DB setup: DB_PATH=%s dir=%s uid=%s gid=%s dir_exists=%s dir_writable=%s",
        DB_PATH, d, os.getuid(), os.getgid(), os.path.isdir(d), os.access(d, os.W_OK),
    )

    try:
        test_conn = sqlite3.connect(DB_PATH)
        test_conn.execute("CREATE TABLE IF NOT EXISTS _write_test(x)")
        test_conn.close()
        log.info("DB_PATH %s is writable, using it.", DB_PATH)
        _active_db_path = DB_PATH
    except Exception as e:
        log.error("DB_PATH %s NOT writable (%s). Falling back to %s — data will NOT persist across deploys until this is fixed!", DB_PATH, e, FALLBACK_DB_PATH)
        _active_db_path = FALLBACK_DB_PATH
    return _active_db_path

def db():
    path = _prepare_db_path()
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_state (
            user_id TEXT PRIMARY KEY,
            data TEXT NOT NULL
        )
    """)
    return conn

def get_state(user_id: str) -> dict:
    conn = db()
    row = conn.execute("SELECT data FROM user_state WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    if not row:
        return {"habits": [], "completions": {}}
    return json.loads(row[0])

def set_state(user_id: str, state: dict):
    conn = db()
    conn.execute(
        "INSERT INTO user_state(user_id, data) VALUES (?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET data=excluded.data",
        (user_id, json.dumps(state)),
    )
    conn.commit()
    conn.close()

def all_user_ids() -> list[str]:
    conn = db()
    rows = conn.execute("SELECT user_id FROM user_state").fetchall()
    conn.close()
    return [r[0] for r in rows]

# ---------------------------------------------------------------- helpers

def now_local() -> datetime:
    return datetime.now(TZ)

def today_str() -> str:
    return now_local().date().isoformat()

def dow_index(d: date) -> int:
    return d.weekday()  # Monday=0 already matches ПН=0 in the frontend

def is_scheduled_today(habit: dict, d: date) -> bool:
    sched = habit.get("schedule", {"type": "daily"})
    if sched.get("type") == "daily":
        return True
    if sched.get("type") == "days":
        return dow_index(d) in sched.get("days", [])
    return True

def is_done(habit: dict, completions: dict, ds: str) -> bool:
    rec = completions.get(habit["id"], {})
    val = rec.get(ds, False if habit["type"] == "check" else 0)
    if habit["type"] == "count":
        return val >= habit.get("target", 1)
    return bool(val)

# ---------------------------------------------------------------- Telegram bot

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()

@dp.message(CommandStart())
async def start_handler(message: Message):
    fresh_url = f"{WEBAPP_URL}?v={int(time.time())}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Открыть трекер", web_app=WebAppInfo(url=fresh_url))
    ]])
    await message.answer(
        "Привет! Это твой трекер привычек. Открывай и отмечай выполненное — "
        "я буду напоминать в течение дня, если что-то забудешь.",
        reply_markup=kb,
    )

async def reminder_tick(scheduler_state: dict):
    """Runs every minute: checks every user's habits against current HH:MM (Moscow time)."""
    now = now_local()
    hhmm = now.strftime("%H:%M")
    today = now.date()
    ds = today_str()

    for user_id in all_user_ids():
        state = get_state(user_id)
        habits = state.get("habits", [])
        completions = state.get("completions", {})

        due = []
        for h in habits:
            if h.get("archived") or h.get("paused"):
                continue
            times = h.get("reminderTimes")
            if not times:
                times = [h["reminderTime"]] if h.get("reminderTime") else []
            if hhmm not in times:
                continue
            if not is_scheduled_today(h, today):
                continue
            if is_done(h, completions, ds):
                continue
            key = f"{user_id}:{h['id']}:{ds}:{hhmm}"
            if key in scheduler_state["sent"]:
                continue
            scheduler_state["sent"].add(key)
            due.append(h)

        if due and bot:
            names = ", ".join(f"{h['emoji']} {h['name']}" for h in due)
            try:
                await bot.send_message(int(user_id), f"Не забудь: {names}")
            except Exception as e:
                log.warning("failed to notify %s: %s", user_id, e)

# ---------------------------------------------------------------- FastAPI app

scheduler_state = {"sent": set()}
scheduler = AsyncIOScheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.add_job(reminder_tick, "cron", second=0, timezone=TZ, args=[scheduler_state])  # every minute at :00, Moscow time
    scheduler.start()
    bot_task = None
    if bot:
        bot_task = asyncio.create_task(dp.start_polling(bot))
    else:
        log.warning("BOT_TOKEN not set — bot disabled, only web app + API will run")
    yield
    if bot_task:
        bot_task.cancel()
    scheduler.shutdown()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def index():
    return FileResponse("static/index.html", headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
    })

@app.get("/api/state")
async def api_get_state(user_id: str):
    return JSONResponse(get_state(user_id))

@app.post("/api/state")
async def api_set_state(request: Request):
    body = await request.json()
    user_id = body.get("user_id")
    state = body.get("state")
    if not user_id or state is None:
        return JSONResponse({"error": "user_id and state required"}, status_code=400)
    set_state(user_id, state)
    return JSONResponse({"ok": True})

app.mount("/static", StaticFiles(directory="static"), name="static")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
