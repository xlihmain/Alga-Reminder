"""
Бот-напоминалка о созвонах для студентов и тьюторов.

За N минут (по умолчанию 10) до занятия бот присылает ссылку
каждому участнику занятия в личку.

Настройки (переменные окружения):
  BOT_TOKEN       — токен от @BotFather (обязательно)
  ADMIN_IDS       — ID админов через запятую, например 12345,67890
  TZ_NAME         — часовой пояс, по умолчанию Asia/Kuala_Lumpur
  REMIND_MINUTES  — за сколько минут напоминать, по умолчанию 10
  DB_PATH         — файл базы, по умолчанию bot.db
"""

import logging
import os
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.environ.get("BOT_TOKEN", "")
ADMINS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Asia/Kuala_Lumpur"))
REMIND_MIN = int(os.environ.get("REMIND_MINUTES", "10"))
DB_PATH = os.environ.get("DB_PATH", "bot.db")

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("bot")

HELP = (
    "Команды админа:\n\n"
    "/add ДАТА ВРЕМЯ ССЫЛКА @участник1 @участник2 [weekly]\n"
    "  пример: /add 16.11 18:00 https://meet.google.com/abc-defg-hij @aliya @tutor_dana\n"
    "  weekly — повторять каждую неделю\n"
    "  дату можно писать 16.11, 16.11.2026 или 2026-11-16\n"
    "  вместо @ника можно указать числовой ID\n\n"
    "/list — ближайшие занятия\n"
    "/del НОМЕР — удалить занятие\n"
    "/users — кто нажал /start\n\n"
    "Важно: каждый студент и тьютор должен один раз нажать /start у бота, "
    "иначе Telegram не даст боту ему написать."
)


# ---------- база ----------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS users ("
            " chat_id INTEGER PRIMARY KEY, username TEXT, name TEXT)"
        )
        c.execute(
            "CREATE TABLE IF NOT EXISTS lessons ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " start_ts TEXT NOT NULL,"      # ISO с часовым поясом
            " link TEXT NOT NULL,"
            " participants TEXT NOT NULL,"  # через пробел: @nick или числовой id
            " weekly INTEGER NOT NULL DEFAULT 0)"
        )


def resolve(token: str):
    """@nick или числовой id -> chat_id (или None, если человек не нажал /start)."""
    if token.lstrip("-").isdigit():
        return int(token)
    with db() as c:
        row = c.execute(
            "SELECT chat_id FROM users WHERE lower(username)=?",
            (token.lstrip("@").lower(),),
        ).fetchone()
    return row["chat_id"] if row else None


# ---------- разбор даты ----------

def parse_dt(date_s: str, time_s: str) -> datetime:
    now = datetime.now(TZ)
    t = datetime.strptime(time_s, "%H:%M").time()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m"):
        try:
            d = datetime.strptime(date_s, fmt)
        except ValueError:
            continue
        if fmt == "%d.%m":
            d = d.replace(year=now.year)
            candidate = datetime.combine(d.date(), t, TZ)
            if candidate < now - timedelta(days=1):  # дата уже прошла — значит следующий год
                d = d.replace(year=now.year + 1)
        return datetime.combine(d.date(), t, TZ)
    raise ValueError("bad date")


def fmt_dt(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%d.%m %H:%M")


# ---------- планирование ----------

def schedule(app: Application, lesson_id: int, start: datetime):
    remind_at = start - timedelta(minutes=REMIND_MIN)
    now = datetime.now(TZ)
    when = remind_at if remind_at > now else now + timedelta(seconds=2)
    app.job_queue.run_once(send_reminder, when=when, data=lesson_id, name=f"lesson-{lesson_id}")


def unschedule(app: Application, lesson_id: int):
    for job in app.job_queue.get_jobs_by_name(f"lesson-{lesson_id}"):
        job.schedule_removal()


async def notify_admins(context: ContextTypes.DEFAULT_TYPE, text: str):
    for admin in ADMINS:
        try:
            await context.bot.send_message(admin, text)
        except Exception as e:  # noqa: BLE001
            log.warning("cannot notify admin %s: %s", admin, e)


async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    lesson_id = context.job.data
    with db() as c:
        row = c.execute("SELECT * FROM lessons WHERE id=?", (lesson_id,)).fetchone()
    if not row:
        return  # занятие удалили

    start = datetime.fromisoformat(row["start_ts"])
    text = (
        f"⏰ Через {REMIND_MIN} минут занятие ({start.astimezone(TZ):%H:%M}).\n"
        f"Ссылка: {row['link']}"
    )

    failed = []
    for token in row["participants"].split():
        chat_id = resolve(token)
        if chat_id is None:
            failed.append(f"{token} (не нажал /start)")
            continue
        try:
            await context.bot.send_message(chat_id, text, disable_web_page_preview=True)
        except Exception as e:  # noqa: BLE001
            failed.append(f"{token} ({e})")

    if failed:
        await notify_admins(
            context,
            f"⚠️ Занятие #{lesson_id} {fmt_dt(start)}: не доставлено → " + ", ".join(failed),
        )

    with db() as c:
        if row["weekly"]:
            nxt = start + timedelta(days=7)
            c.execute("UPDATE lessons SET start_ts=? WHERE id=?", (nxt.isoformat(), lesson_id))
            schedule(context.application, lesson_id, nxt)
        else:
            c.execute("DELETE FROM lessons WHERE id=?", (lesson_id,))


def restore_jobs(app: Application):
    """После перезапуска бота заново ставит все напоминания."""
    now = datetime.now(TZ)
    with db() as c:
        rows = c.execute("SELECT * FROM lessons").fetchall()
        for row in rows:
            start = datetime.fromisoformat(row["start_ts"])
            if start <= now:
                if not row["weekly"]:
                    c.execute("DELETE FROM lessons WHERE id=?", (row["id"],))
                    continue
                while start <= now:
                    start += timedelta(days=7)
                c.execute("UPDATE lessons SET start_ts=? WHERE id=?", (start.isoformat(), row["id"]))
            schedule(app, row["id"], start)
    log.info("restored %d lessons", len(rows))


# ---------- команды ----------

def is_admin(update: Update) -> bool:
    return update.effective_user.id in ADMINS


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    with db() as c:
        c.execute(
            "INSERT INTO users(chat_id, username, name) VALUES(?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET username=excluded.username, name=excluded.name",
            (u.id, u.username or "", u.full_name),
        )
    msg = (
        "Готово! Теперь я буду присылать ссылку на занятие "
        f"за {REMIND_MIN} минут до начала.\n\nВаш ID: {u.id}"
    )
    if not u.username:
        msg += "\nУ вас нет @ника — передайте этот ID администратору."
    if is_admin(update):
        msg += "\n\n" + HELP
    await update.message.reply_text(msg)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update):
        await update.message.reply_text(HELP)


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    args = context.args
    weekly = any(a.lower() in ("weekly", "еженедельно") for a in args)
    args = [a for a in args if a.lower() not in ("weekly", "еженедельно")]

    if len(args) < 4:
        await update.message.reply_text("Не хватает данных.\n\n" + HELP)
        return

    date_s, time_s, link, *people = args
    try:
        start = parse_dt(date_s, time_s)
    except ValueError:
        await update.message.reply_text("Не понял дату или время. Пример: /add 16.11 18:00 ССЫЛКА @ник")
        return
    if start <= datetime.now(TZ):
        await update.message.reply_text("Это время уже прошло.")
        return
    if not link.startswith("http"):
        await update.message.reply_text("Третьим должна идти ссылка (https://...).")
        return

    with db() as c:
        cur = c.execute(
            "INSERT INTO lessons(start_ts, link, participants, weekly) VALUES(?,?,?,?)",
            (start.isoformat(), link, " ".join(people), int(weekly)),
        )
        lesson_id = cur.lastrowid
    schedule(context.application, lesson_id, start)

    not_registered = [p for p in people if resolve(p) is None]
    reply = f"✅ Занятие #{lesson_id}: {fmt_dt(start)}{' (каждую неделю)' if weekly else ''}\n" + " ".join(people)
    if not_registered:
        reply += (
            "\n\n⚠️ Ещё не нажали /start у бота: " + ", ".join(not_registered)
            + "\nПусть нажмут до занятия, иначе напоминание не дойдёт."
        )
    await update.message.reply_text(reply, disable_web_page_preview=True)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    with db() as c:
        rows = c.execute("SELECT * FROM lessons ORDER BY start_ts").fetchall()
    if not rows:
        await update.message.reply_text("Занятий нет.")
        return
    lines = [
        f"#{r['id']}  {fmt_dt(datetime.fromisoformat(r['start_ts']))}"
        f"{' 🔁' if r['weekly'] else ''}  {r['participants']}"
        for r in rows
    ]
    await update.message.reply_text("\n".join(lines) + "\n\n🔁 — каждую неделю")


async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not context.args or not context.args[0].lstrip("#").isdigit():
        await update.message.reply_text("Пример: /del 3")
        return
    lesson_id = int(context.args[0].lstrip("#"))
    with db() as c:
        deleted = c.execute("DELETE FROM lessons WHERE id=?", (lesson_id,)).rowcount
    unschedule(context.application, lesson_id)
    await update.message.reply_text("Удалено." if deleted else "Такого занятия нет.")


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    with db() as c:
        rows = c.execute("SELECT * FROM users ORDER BY name").fetchall()
    if not rows:
        await update.message.reply_text("Пока никто не нажал /start.")
        return
    lines = [f"{r['name']} — @{r['username']}" if r["username"] else f"{r['name']} — ID {r['chat_id']}" for r in rows]
    await update.message.reply_text("\n".join(lines))


async def post_init(app: Application):
    restore_jobs(app)


def main():
    if not TOKEN:
        raise SystemExit("Укажите BOT_TOKEN")
    if not ADMINS:
        log.warning("ADMIN_IDS пуст — команды админа работать не будут. Узнайте свой ID через /start.")
    init_db()
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(CommandHandler("users", cmd_users))
    app.run_polling()


if __name__ == "__main__":
    main()
