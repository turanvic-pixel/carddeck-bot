import asyncio
import logging
import os

import aiohttp
from aiohttp import web
from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from storage import CardStorage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("carddeck-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO = os.environ.get("GITHUB_REPO", "carddeck-bot")
EXTERNAL_URL = os.environ.get("EXTERNAL_URL")  # напр. https://carddeck-bot.onrender.com
PORT = int(os.environ.get("PORT", 10000))

storage = CardStorage(GITHUB_TOKEN, GITHUB_REPO)

DRAW_BUTTON = ReplyKeyboardMarkup(
    [["💎 Открыть жемчужину души"]],
    resize_keyboard=True,
    is_persistent=True,
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Кнопка внизу экрана всегда под рукой — жми и вытягивай карточку.",
        reply_markup=DRAW_BUTTON,
    )


async def draw_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    card = storage.next_card_for_user(update.effective_user.id)
    if card is None:
        await update.message.reply_text("Пока нет ни одной карточки в коллекции.", reply_markup=DRAW_BUTTON)
        return
    caption = f"Карточка #{card['id']}" if update.effective_user.id == ADMIN_ID else None
    if card.get("kind") == "document":
        await update.message.reply_document(document=card["file_id"], caption=caption, reply_markup=DRAW_BUTTON)
    else:
        await update.message.reply_photo(photo=card["file_id"], caption=caption, reply_markup=DRAW_BUTTON)


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
    application.add_handler(MessageHandler(filters.Regex("^💎 Открыть жемчужину души$"), draw_card))
    application.add_handler(MessageHandler(filters.PHOTO & filters.User(ADMIN_ID), admin_add_card_photo))
    application.add_handler(MessageHandler(filters.Document.IMAGE & filters.User(ADMIN_ID), admin_add_card_document))
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.User(ADMIN_ID), admin_ignore_non_admin_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, start))

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
