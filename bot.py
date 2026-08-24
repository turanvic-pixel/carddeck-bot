import asyncio
import datetime
import io
import logging
import os
import re
import zipfile

import aiohttp
from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from storage import CardStorage, FavoritesStore, ReminderStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("carddeck-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO = os.environ.get("GITHUB_REPO", "carddeck-bot")
EXTERNAL_URL = os.environ.get("EXTERNAL_URL")  # напр. https://carddeck-bot.onrender.com
PORT = int(os.environ.get("PORT", 10000))

storage = CardStorage(GITHUB_TOKEN, GITHUB_REPO)
favorites = FavoritesStore(GITHUB_TOKEN, GITHUB_REPO)
reminders = ReminderStore(GITHUB_TOKEN, GITHUB_REPO)

REMINDER_BUTTON_TEXT = "⏰ Напоминания"
FAVORITES_BUTTON_TEXT = "⭐ Избранное"
STATS_BUTTON_TEXT = "📊 Статистика"

DRAW_BUTTON = ReplyKeyboardMarkup(
    [
        ["💎 Открыть жемчужину души"],
        [FAVORITES_BUTTON_TEXT, STATS_BUTTON_TEXT],
        [REMINDER_BUTTON_TEXT],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

# статистика просмотров — в памяти процесса (как и очередь карточек), сбрасывается при рестарте Render
_stats_data: dict[int, dict[str, int]] = {}


def record_view(user_id: int):
    today = datetime.date.today().isoformat()
    user_stats = _stats_data.setdefault(user_id, {})
    user_stats[today] = user_stats.get(today, 0) + 1


def get_stats(user_id: int):
    today = datetime.date.today()
    user_stats = _stats_data.get(user_id, {})
    total = sum(user_stats.values())
    day = user_stats.get(today.isoformat(), 0)
    week_start = today - datetime.timedelta(days=today.weekday())
    week = sum(
        v for d, v in user_stats.items()
        if week_start <= datetime.date.fromisoformat(d) <= today
    )
    month = sum(
        v for d, v in user_stats.items()
        if datetime.date.fromisoformat(d).year == today.year
        and datetime.date.fromisoformat(d).month == today.month
    )
    return total, day, week, month


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total, day, week, month = get_stats(update.effective_user.id)
    await update.message.reply_text(
        "📊 Твоя статистика просмотров:\n\n"
        f"Всего: {total}\n"
        f"За день: {day}\n"
        f"За неделю: {week}\n"
        f"За месяц: {month}"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text(
        "Привет! Кнопка внизу экрана всегда под рукой — жми и вытягивай карточку.",
        reply_markup=DRAW_BUTTON,
    )
    try:
        chat = await context.bot.get_chat(update.effective_chat.id)
        if chat.pinned_message is None:
            await context.bot.pin_chat_message(
                chat_id=update.effective_chat.id, message_id=msg.message_id, disable_notification=True
            )
    except Exception as e:
        logger.warning("Не удалось закрепить сообщение: %s", e)


def _fav_keyboard(card_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❤️ Сохранить", callback_data=f"fav:{card_id}")]])


async def draw_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    card = storage.next_card_for_user(update.effective_user.id)
    if card is None:
        await update.message.reply_text("Пока нет ни одной карточки в коллекции.", reply_markup=DRAW_BUTTON)
        return
    record_view(update.effective_user.id)
    caption = f"Карточка #{card['id']}" if update.effective_user.id == ADMIN_ID else None
    kb = _fav_keyboard(card["id"])
    if card.get("kind") == "document":
        await update.message.reply_document(document=card["file_id"], caption=caption, reply_markup=kb)
    else:
        await update.message.reply_photo(photo=card["file_id"], caption=caption, reply_markup=kb)


async def favorite_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    card_id = int(query.data.split(":")[1])
    added = favorites.add(update.effective_user.id, card_id)
    await query.answer("Сохранено в избранное ❤️" if added else "Уже в избранном")


async def unfavorite_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    card_id = int(query.data.split(":")[1])
    favorites.remove(update.effective_user.id, card_id)
    await query.answer("Убрано из избранного")
    await query.edit_message_reply_markup(reply_markup=None)


async def favorites_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ids = favorites.list_for_user(update.effective_user.id)
    if not ids:
        await update.message.reply_text("Пока нет сохранённых карточек. Жми ❤️ Сохранить под карточкой.")
        return
    await update.message.reply_text(f"Сохранено карточек: {len(ids)}. Отправляю (до 20 за раз)...")
    for card_id in ids[:20]:
        card = next((c for c in storage.cards if c["id"] == card_id), None)
        if card is None:
            continue
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Убрать из избранного", callback_data=f"unfav:{card_id}")]])
        if card.get("kind") == "document":
            await update.message.reply_document(document=card["file_id"], reply_markup=kb)
        else:
            await update.message.reply_photo(photo=card["file_id"], reply_markup=kb)
    if len(ids) > 20:
        await update.message.reply_text(f"И ещё {len(ids) - 20} в избранном — вызови /favorites ещё раз позже.")


async def admin_add_card_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    file_id = update.message.photo[-1].file_id
    new_id = storage.add_card(file_id, kind="photo")
    await update.message.reply_text(
        f"Добавлено! Карточка #{new_id}. Всего карточек: {storage.count()}.\n"
        f"Если картинка выглядит размытой — отправляй её как «Файл» (без сжатия), а не как «Фото».",
        reply_markup=DRAW_BUTTON,
    )


async def admin_add_card_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    doc = update.message.document
    if not doc.mime_type or not doc.mime_type.startswith("image/"):
        return
    new_id = storage.add_card(doc.file_id, kind="document")
    await update.message.reply_text(
        f"Добавлено (без сжатия)! Карточка #{new_id}. Всего карточек: {storage.count()}.",
        reply_markup=DRAW_BUTTON,
    )


async def admin_ignore_non_admin_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        return
    await update.message.reply_text("Фото принимает только администратор коллекции.")


async def count_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    ids = storage.list_ids()
    preview = ", ".join(str(i) for i in ids[:30])
    more = f" и ещё {len(ids) - 30}" if len(ids) > 30 else ""
    await update.message.reply_text(f"Всего карточек: {storage.count()}.\nНомера: {preview}{more}")


async def card_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Формат: /card <номер карточки>, например /card 3")
        return
    card_id = int(context.args[0])
    card = next((c for c in storage.cards if c["id"] == card_id), None)
    if card is None:
        await update.message.reply_text(f"Карточка #{card_id} не найдена.")
        return
    caption = f"Карточка #{card_id}. Чтобы удалить: /delete {card_id}"
    if card.get("kind") == "document":
        await update.message.reply_document(document=card["file_id"], caption=caption)
    else:
        await update.message.reply_photo(photo=card["file_id"], caption=caption)


async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Формат: /delete <номер карточки>, например /delete 3")
        return
    card_id = int(context.args[0])
    ok = storage.delete_card(card_id)
    if ok:
        await update.message.reply_text(f"Карточка #{card_id} удалена. Всего карточек: {storage.count()}.")
    else:
        await update.message.reply_text(f"Карточка #{card_id} не найдена.")


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Твой Telegram ID: {update.effective_user.id}")


MAX_ZIP_BYTES = 40 * 1024 * 1024  # запас от лимита Telegram в 50 МБ на файл


async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not storage.cards:
        await update.message.reply_text("Карточек пока нет.")
        return
    await update.message.reply_text(f"Начинаю выгрузку {storage.count()} карточек, это может занять время...")

    buf = io.BytesIO()
    zf = zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED)
    part = 1
    count_in_zip = 0
    failed = []

    for card in storage.cards:
        try:
            tg_file = await context.bot.get_file(card["file_id"])
            data = await tg_file.download_as_bytearray()
        except Exception as e:
            logger.warning("Не удалось скачать карточку #%s: %s", card["id"], e)
            failed.append(card["id"])
            continue
        zf.writestr(f"card_{card['id']:04d}.jpg", bytes(data))
        count_in_zip += 1
        if buf.tell() > MAX_ZIP_BYTES:
            zf.close()
            buf.seek(0)
            await update.message.reply_document(document=buf, filename=f"cards_part{part}.zip")
            part += 1
            buf = io.BytesIO()
            zf = zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED)
            count_in_zip = 0

    zf.close()
    if count_in_zip > 0:
        buf.seek(0)
        await update.message.reply_document(document=buf, filename=f"cards_part{part}.zip")

    msg = "Готово! Все карточки отправлены архивом(-ами)."
    if failed:
        msg += f"\nНе удалось скачать: {failed}"
    await update.message.reply_text(msg)


def schedule_reminder(application: Application, user_id: int, hour: int, minute: int):
    for job in application.job_queue.get_jobs_by_name(str(user_id)):
        job.schedule_removal()
    application.job_queue.run_daily(
        send_daily_card,
        time=datetime.time(hour=hour, minute=minute, tzinfo=datetime.timezone.utc),
        chat_id=user_id,
        name=str(user_id),
    )


async def send_daily_card(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    card = storage.next_card_for_user(chat_id)
    if card is None:
        return
    record_view(chat_id)
    kb = _fav_keyboard(card["id"])
    if card.get("kind") == "document":
        await context.bot.send_document(chat_id=chat_id, document=card["file_id"], caption="🌅 Карточка дня", reply_markup=kb)
    else:
        await context.bot.send_photo(chat_id=chat_id, photo=card["file_id"], caption="🌅 Карточка дня", reply_markup=kb)


TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


REMINDER_TIME_OPTIONS = ["08:00", "10:00", "12:00", "18:00", "21:00"]


def _reminder_keyboard(user_id: int) -> InlineKeyboardMarkup:
    current = reminders.all().get(str(user_id))
    row = []
    for t in REMINDER_TIME_OPTIONS:
        hour, minute = map(int, t.split(":"))
        msk = f"{(hour + 3) % 24:02d}:{minute:02d} МСК"
        label = f"✅ {msk}" if t == current else msk
        row.append(InlineKeyboardButton(label, callback_data=f"remind_set:{t}"))
    rows = [row[:2], row[2:4], row[4:]]
    rows.append([InlineKeyboardButton("❌ Выключить напоминание", callback_data="remind_off")])
    return InlineKeyboardMarkup(rows)


async def reminder_menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current = reminders.all().get(str(update.effective_user.id))
    text = "Выбери время ежедневной карточки:"
    if current:
        hour, minute = map(int, current.split(":"))
        text += f"\n\nСейчас включено на {(hour + 3) % 24:02d}:{minute:02d} МСК."
    await update.message.reply_text(text, reply_markup=_reminder_keyboard(update.effective_user.id))


async def reminder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.data == "remind_off":
        ok = reminders.remove(update.effective_user.id)
        for job in context.application.job_queue.get_jobs_by_name(str(update.effective_user.id)):
            job.schedule_removal()
        await query.answer("Напоминание выключено" if ok else "Напоминание и так было выключено")
        await query.edit_message_text(
            "Напоминание выключено.", reply_markup=_reminder_keyboard(update.effective_user.id)
        )
        return

    _, time_str = query.data.split(":", 1)
    hour, minute = map(int, time_str.split(":"))
    reminders.set(update.effective_user.id, time_str)
    schedule_reminder(context.application, update.effective_user.id, hour, minute)
    await query.answer(f"Включено на {(hour + 3) % 24:02d}:{minute:02d} МСК")
    await query.edit_message_text(
        f"Готово! Буду присылать карточку каждый день в {(hour + 3) % 24:02d}:{minute:02d} МСК.",
        reply_markup=_reminder_keyboard(update.effective_user.id),
    )


async def remind_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Формат: /remind_on 09:00 (время по UTC).")
        return
    m = TIME_RE.match(context.args[0])
    if not m:
        await update.message.reply_text("Не понял время. Формат ЧЧ:ММ, например /remind_on 09:00 (UTC).")
        return
    hour, minute = int(m.group(1)), int(m.group(2))
    reminders.set(update.effective_user.id, f"{hour:02d}:{minute:02d}")
    schedule_reminder(context.application, update.effective_user.id, hour, minute)
    await update.message.reply_text(
        f"Готово! Буду присылать карточку каждый день в {hour:02d}:{minute:02d} по UTC "
        f"(по Москве это ~{(hour + 3) % 24:02d}:{minute:02d} МСК)."
    )


async def remind_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ok = reminders.remove(update.effective_user.id)
    for job in context.application.job_queue.get_jobs_by_name(str(update.effective_user.id)):
        job.schedule_removal()
    await update.message.reply_text("Напоминания отключены." if ok else "У тебя не было включённых напоминаний.")


async def health(request):
    return web.Response(text="OK")


async def self_ping():
    if not EXTERNAL_URL:
        logger.warning("EXTERNAL_URL не задан — автопинг выключен, бот может засыпать")
        return
    async with aiohttp.ClientSession() as session:
        while True:
            await asyncio.sleep(600)  # 10 минут
            try:
                async with session.get(EXTERNAL_URL, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    logger.info("self-ping %s -> %s", EXTERNAL_URL, r.status)
            except Exception as e:
                logger.warning("self-ping failed: %s", e)


async def main():
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("count", count_cmd))
    application.add_handler(CommandHandler("card", card_cmd))
    application.add_handler(CommandHandler("delete", delete_cmd))
    application.add_handler(CommandHandler("whoami", whoami))
    application.add_handler(CommandHandler("export", export_cmd))
    application.add_handler(CommandHandler("favorites", favorites_cmd))
    application.add_handler(CommandHandler("remind_on", remind_on_cmd))
    application.add_handler(CommandHandler("remind_off", remind_off_cmd))
    application.add_handler(CallbackQueryHandler(favorite_callback, pattern="^fav:"))
    application.add_handler(CallbackQueryHandler(unfavorite_callback, pattern="^unfav:"))
    application.add_handler(CallbackQueryHandler(reminder_callback, pattern="^remind_"))
    application.add_handler(MessageHandler(filters.Regex("^💎 Открыть жемчужину души$"), draw_card))
    application.add_handler(MessageHandler(filters.Regex(f"^{re.escape(FAVORITES_BUTTON_TEXT)}$"), favorites_cmd))
    application.add_handler(MessageHandler(filters.Regex(f"^{re.escape(STATS_BUTTON_TEXT)}$"), stats_cmd))
    application.add_handler(MessageHandler(filters.Regex(f"^{re.escape(REMINDER_BUTTON_TEXT)}$"), reminder_menu_cmd))
    application.add_handler(MessageHandler(filters.PHOTO & filters.User(ADMIN_ID), admin_add_card_photo))
    application.add_handler(MessageHandler(filters.Document.IMAGE & filters.User(ADMIN_ID), admin_add_card_document))
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.User(ADMIN_ID), admin_ignore_non_admin_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, start))

    for uid_str, time_str in reminders.all().items():
        hour, minute = map(int, time_str.split(":"))
        schedule_reminder(application, int(uid_str), hour, minute)

    web_app = web.Application()
    web_app.router.add_get("/", health)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info("Веб-сервер запущен на порту %s", PORT)

    asyncio.create_task(self_ping())

    async with application:
        await application.start()
        await application.updater.start_polling(drop_pending_updates=True)
        logger.info("Бот запущен, ждём сообщений...")
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
