# Card Deck Bot

Telegram-бот со случайными карточками (фото + текст).

## Переменные окружения (задаются в Render → Environment)

| Ключ | Значение |
|---|---|
| `BOT_TOKEN` | токен от @BotFather |
| `ADMIN_ID` | твой Telegram ID (числом) |
| `GITHUB_TOKEN` | токен GitHub (для хранения карточек) |
| `GITHUB_REPO` | `carddeck-bot` |
| `EXTERNAL_URL` | адрес сервиса на Render, напр. `https://carddeck-bot.onrender.com` |

## Как пользоваться

- Любой пользователь: `/start` → кнопка «🎴 Выбрать карточку».
- Администратор: отправить боту фото **с подписью** (текст карточки в подписи к фото) — карточка добавится в коллекцию.
- `/count` — сколько карточек всего (только админ).
- `/whoami` — узнать свой Telegram ID.

## Запуск локально

```bash
pip install -r requirements.txt
export BOT_TOKEN=...
export ADMIN_ID=...
export GITHUB_TOKEN=...
export GITHUB_REPO=carddeck-bot
python bot.py
```

## Почему карточки хранятся в GitHub, а не в SQLite

Бесплатный Render стирает диск сервиса при каждом рестарте/редеплое.
SQLite-файл в этом случае теряется. `cards.json` в GitHub-репозитории — нет.
