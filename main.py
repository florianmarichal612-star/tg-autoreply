"""
Telegram userbot с AI-ответами на базе Claude.

Что делает:
- Слушает входящие ЛС от твоего личного аккаунта
- Игнорирует чёрный список (директор, коллеги, семья)
- Оценивает важность сообщения и генерирует ответ через Claude
- Важные сообщения пересылает тебе в "Избранное" (Saved Messages)
- На остальные отвечает от твоего имени с человекоподобной задержкой

Управление в чате с самим собой (Saved Messages):
  /pause      — отключить автоответы на 1 час
  /pause 6h   — отключить на 6 часов
  /resume     — включить обратно
  /status     — статус бота
"""

import asyncio
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.types import User

from config import BLACKLIST_IDS, VIP_IDS, KEYWORDS, SYSTEM_PROMPT

load_dotenv()

# --- Настройки ---
API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SESSION_NAME = os.environ.get("TG_SESSION", "/data/veronika")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

# Задержка перед ответом (сек) — чтобы не выглядело как бот
MIN_DELAY = int(os.environ.get("MIN_DELAY", 20))
MAX_DELAY = int(os.environ.get("MAX_DELAY", 90))

# Сколько последних сообщений в чате брать для контекста
CONTEXT_MESSAGES = 10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("tg-autoreply")

# --- Клиенты ---
client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
claude = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

# --- Runtime state ---
paused_until: datetime | None = None
my_id: int | None = None
processing: set[int] = set()  # чтобы не дублировать обработку


async def is_paused() -> bool:
    return paused_until is not None and datetime.now() < paused_until


async def ask_claude(chat_history: list[dict], sender_name: str) -> dict:
    """
    Возвращает {"important": bool, "reason": str, "reply": str}.
    Если important=True, reply игнорируется — переcылаем тебе.
    """
    system = SYSTEM_PROMPT.format(sender_name=sender_name)
    user_msg = (
        "Вот последние сообщения в чате (последнее — новое, на которое нужно "
        "среагировать):\n\n"
        + "\n".join(f"[{m['from']}]: {m['text']}" for m in chat_history)
        + "\n\nОтветь СТРОГО валидным JSON вида:\n"
        + '{"important": true|false, "reason": "коротко почему", "reply": "текст ответа или null"}'
    )
    resp = await claude.messages.create(
        model=MODEL,
        max_tokens=800,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = resp.content[0].text.strip()
    # Иногда модель оборачивает в ```json ... ```
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    return json.loads(text)


def keyword_hit(text: str) -> str | None:
    """Быстрая проверка на триггерные слова. Возвращает найденное слово или None."""
    low = text.lower()
    for kw in KEYWORDS:
        if kw.lower() in low:
            return kw
    return None


async def gather_context(chat_id: int, limit: int = CONTEXT_MESSAGES) -> list[dict]:
    """Забирает последние N сообщений в чате для контекста."""
    messages = []
    async for m in client.iter_messages(chat_id, limit=limit):
        if not m.text:
            continue
        sender = "Я" if m.out else "Собеседник"
        messages.append({"from": sender, "text": m.text, "date": m.date})
    messages.reverse()
    return messages


async def forward_to_saved(chat_id: int, msg_id: int, sender_name: str, reason: str):
    """Пересылает сообщение в Saved Messages с пояснением."""
    await client.forward_messages("me", msg_id, chat_id)
    await client.send_message(
        "me",
        f"⚠️ Важное от **{sender_name}**\nПричина: {reason}\n"
        f"👉 [Открыть чат](tg://user?id={chat_id})",
    )


# --- Обработчик команд в Saved Messages ---
@client.on(events.NewMessage(outgoing=True, chats="me"))
async def handle_commands(event):
    global paused_until
    text = (event.raw_text or "").strip()

    if text.startswith("/pause"):
        parts = text.split(maxsplit=1)
        duration = parts[1] if len(parts) > 1 else "1h"
        match = re.match(r"(\d+)(h|m)", duration)
        hours = 1
        if match:
            n, unit = int(match.group(1)), match.group(2)
            hours = n if unit == "h" else n / 60
        paused_until = datetime.now() + timedelta(hours=hours)
        await event.reply(f"⏸ Автоответы выключены до {paused_until:%H:%M %d.%m}")

    elif text.startswith("/resume"):
        paused_until = None
        await event.reply("▶️ Автоответы снова включены")

    elif text.startswith("/status"):
        if await is_paused():
            await event.reply(f"⏸ На паузе до {paused_until:%H:%M %d.%m}")
        else:
            await event.reply("✅ Работаю")


# --- Основной обработчик входящих ---
@client.on(events.NewMessage(incoming=True))
async def handle_incoming(event):
    if not event.is_private:
        return  # только ЛС, никаких групп

    sender = await event.get_sender()
    if not isinstance(sender, User) or sender.bot:
        return

    if sender.id == my_id:
        return
    if sender.id in BLACKLIST_IDS:
        log.info(f"Пропуск (blacklist): {sender.first_name} ({sender.id})")
        return
    if await is_paused():
        log.info(f"На паузе, пропуск: {sender.first_name}")
        return
    if event.chat_id in processing:
        return

    processing.add(event.chat_id)
    try:
        # Небольшая пауза, чтобы собрать возможные follow-up сообщения
        await asyncio.sleep(random.randint(MIN_DELAY, MAX_DELAY))

        sender_name = sender.first_name or "собеседник"
        text = event.raw_text or ""

        # --- Быстрая проверка на VIP и ключевые слова ---
        if sender.id in VIP_IDS:
            await forward_to_saved(
                event.chat_id, event.id, sender_name, "VIP-контакт"
            )
            return

        kw = keyword_hit(text)
        if kw:
            await forward_to_saved(
                event.chat_id, event.id, sender_name, f"Ключевое слово: «{kw}»"
            )
            return

        # --- Полноценная оценка через Claude ---
        history = await gather_context(event.chat_id)
        try:
            decision = await ask_claude(history, sender_name)
        except Exception as e:
            log.exception("Claude API failed")
            await forward_to_saved(
                event.chat_id, event.id, sender_name, f"Ошибка AI: {e}"
            )
            return

        if decision.get("important"):
            await forward_to_saved(
                event.chat_id,
                event.id,
                sender_name,
                decision.get("reason", "Claude оценил как важное"),
            )
            return

        reply = decision.get("reply")
        if reply:
            # Имитируем набор текста
            async with client.action(event.chat_id, "typing"):
                await asyncio.sleep(min(len(reply) * 0.05, 8))
            await event.reply(reply)
            log.info(f"Ответил {sender_name}: {reply[:60]}...")
    finally:
        processing.discard(event.chat_id)


async def main():
    global my_id
    await client.start()
    me = await client.get_me()
    my_id = me.id
    log.info(f"Бот запущен от имени: {me.first_name} (id={me.id})")
    await client.send_message(
        "me",
        "🤖 AI-автоответчик запущен.\nКоманды: /pause, /pause 6h, /resume, /status",
    )
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
