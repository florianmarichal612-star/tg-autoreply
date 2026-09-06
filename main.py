"""
Telegram AI-автоответчик для Вероники.
Два клиента в одном процессе:
- user_client: userbot от лица Вероники (Telethon + StringSession)
- bot_client: бот-помощник с inline-кнопками

Всё управление через кнопки в чате с ботом-помощником.
Состояние (blacklist, vip, пауза) хранится в /data/state.json.
"""

import asyncio
import json
import logging
import os
import random
import re
from datetime import datetime, timedelta
from typing import Optional

from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from telethon import Button, TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import User

from config import KEYWORDS, SYSTEM_PROMPT

load_dotenv()

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
MIN_DELAY = int(os.environ.get("MIN_DELAY", 20))
MAX_DELAY = int(os.environ.get("MAX_DELAY", 90))
CONTEXT_MESSAGES = 10
STATE_PATH = os.environ.get("STATE_PATH", "/data/state.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("tg-autoreply")

_s = os.environ.get("TG_SESSION_STRING", "").strip()
SESSION_NAME = os.environ.get("TG_SESSION", "/data/veronika")
user_client = TelegramClient(
    StringSession(_s) if _s else SESSION_NAME, API_ID, API_HASH
)
bot_client = TelegramClient("bot", API_ID, API_HASH)
claude = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

my_id: Optional[int] = None
processing: set = set()
awaiting: dict = {}


def load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {"blacklist": [], "vip": [], "paused_until": None}
    try:
        with open(STATE_PATH) as fh:
            data = json.load(fh)
        data.setdefault("blacklist", [])
        data.setdefault("vip", [])
        data.setdefault("paused_until", None)
        return data
    except Exception:
        log.exception("Не смог прочитать state, стартую с нуля")
        return {"blacklist": [], "vip": [], "paused_until": None}


def save_state():
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


state = load_state()


def is_paused() -> bool:
    if not state.get("paused_until"):
        return False
    return datetime.fromisoformat(state["paused_until"]) > datetime.now()


def paused_until_str() -> str:
    if not state.get("paused_until"):
        return ""
    return datetime.fromisoformat(state["paused_until"]).strftime("%H:%M %d.%m")


def main_menu_kb():
    return [
        [Button.inline("⏸ 30 мин", b"pause:0.5"), Button.inline("⏸ 2 часа", b"pause:2")],
        [Button.inline("⏸ 6 часов", b"pause:6"), Button.inline("⏸ 24 часа", b"pause:24")],
        [Button.inline("▶️ Возобновить", b"resume"), Button.inline("📊 Статус", b"status")],
        [Button.inline("🚫 Blacklist", b"bl:menu"), Button.inline("⭐ VIP", b"vip:menu")],
    ]


def list_menu_kb(prefix: str):
    return [
        [Button.inline("➕ Добавить (перешли сообщ.)", f"{prefix}:add".encode())],
        [Button.inline("📋 Показать список", f"{prefix}:show".encode())],
        [Button.inline("◀️ Назад", b"back")],
    ]


def status_text() -> str:
    if is_paused():
        head = f"⏸ На паузе до {paused_until_str()}"
    else:
        head = "✅ Работаю"
    return (
        f"{head}\n\n"
        f"🚫 Blacklist: {len(state['blacklist'])} чел.\n"
        f"⭐ VIP: {len(state['vip'])} чел.\n"
        f"🔑 Ключевых слов: {len(KEYWORDS)}"
    )


async def ask_claude(chat_history, sender_name):
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
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    return json.loads(text)


def keyword_hit(text: str):
    low = text.lower()
    for kw in KEYWORDS:
        if kw.lower() in low:
            return kw
    return None


async def gather_context(chat_id, limit=CONTEXT_MESSAGES):
    messages = []
    async for m in user_client.iter_messages(chat_id, limit=limit):
        if not m.text:
            continue
        sender = "Я" if m.out else "Собеседник"
        messages.append({"from": sender, "text": m.text})
    messages.reverse()
    return messages


async def notify_admin(chat_id: int, msg_id: int, sender_name: str, reason: str):
    try:
        await user_client.forward_messages(my_id, msg_id, chat_id)
    except Exception:
        log.exception("forward_messages failed")
    try:
        await bot_client.send_message(
            my_id,
            f"⚠️ **Важное от {sender_name}**\n"
            f"Причина: {reason}\n"
            f"[Открыть чат](tg://user?id={chat_id})",
            parse_mode="md",
        )
    except Exception:
        log.exception("bot notify failed")


@user_client.on(events.NewMessage(incoming=True))
async def on_incoming(event):
    if not event.is_private:
        return
    sender = await event.get_sender()
    if not isinstance(sender, User) or sender.bot:
        return
    if sender.id == my_id:
        return
    if sender.id in state["blacklist"]:
        log.info(f"Пропуск (blacklist): {sender.first_name} ({sender.id})")
        return
    if is_paused():
        return
    if event.chat_id in processing:
        return

    processing.add(event.chat_id)
    try:
        await asyncio.sleep(random.randint(MIN_DELAY, MAX_DELAY))
        sender_name = sender.first_name or "собеседник"
        text = event.raw_text or ""

        if sender.id in state["vip"]:
            await notify_admin(event.chat_id, event.id, sender_name, "VIP-контакт")
            return

        kw = keyword_hit(text)
        if kw:
            await notify_admin(
                event.chat_id, event.id, sender_name, f"Ключевое слово: «{kw}»"
            )
            return

        history = await gather_context(event.chat_id)
        try:
            decision = await ask_claude(history, sender_name)
        except Exception as e:
            log.exception("Claude API failed")
            await notify_admin(event.chat_id, event.id, sender_name, f"Ошибка AI: {e}")
            return

        if decision.get("important"):
            await notify_admin(
                event.chat_id,
                event.id,
                sender_name,
                decision.get("reason", "Claude оценил как важное"),
            )
            return

        reply = decision.get("reply")
        if reply:
            async with user_client.action(event.chat_id, "typing"):
                await asyncio.sleep(min(len(reply) * 0.05, 8))
            await event.reply(reply)
            log.info(f"Ответил {sender_name}: {reply[:60]}...")
    finally:
        processing.discard(event.chat_id)


def only_admin(handler):
    async def wrap(event):
        if event.sender_id != my_id:
            return
        return await handler(event)
    return wrap


@bot_client.on(events.NewMessage(pattern="/start"))
@only_admin
async def on_start(event):
    await event.reply(
        "🤖 **Панель управления автоответчиком**\n\n" + status_text(),
        buttons=main_menu_kb(),
        parse_mode="md",
    )


@bot_client.on(events.NewMessage(pattern="/menu"))
@only_admin
async def on_menu(event):
    await event.reply(
        "🤖 **Панель управления**\n\n" + status_text(),
        buttons=main_menu_kb(),
        parse_mode="md",
    )


@bot_client.on(events.NewMessage(incoming=True))
@only_admin
async def on_any_message(event):
    if event.text and event.text.startswith("/"):
        return

    kind = None
    if awaiting.get("blacklist"):
        kind = "blacklist"
    elif awaiting.get("vip"):
        kind = "vip"
    if not kind:
        return

    fwd = event.message.fwd_from
    if not fwd or not fwd.from_id:
        await event.reply(
            "Не вижу источник пересланного сообщения. Возможно, у отправителя "
            "закрыты пересылки. Добавь тогда его ID вручную через @userinfobot "
            "(пока не реализовано в кнопках)."
        )
        return

    try:
        uid = fwd.from_id.user_id
    except AttributeError:
        await event.reply("Это не пользователь. Пришли пересланное сообщение от человека.")
        return

    if uid in state[kind]:
        await event.reply(f"Уже в списке {kind}.")
    else:
        state[kind].append(uid)
        save_state()
        try:
            ent = await user_client.get_entity(uid)
            name = ent.first_name or "(без имени)"
        except Exception:
            name = f"id={uid}"
        await event.reply(f"✅ Добавлен в {kind}: **{name}** (id={uid})", parse_mode="md")

    awaiting[kind] = False


@bot_client.on(events.CallbackQuery)
@only_admin
async def on_callback(event):
    data = event.data.decode()

    if data.startswith("pause:"):
        hours = float(data.split(":", 1)[1])
        state["paused_until"] = (datetime.now() + timedelta(hours=hours)).isoformat()
        save_state()
        await event.edit(
            f"⏸ Автоответы выключены до **{paused_until_str()}**",
            buttons=main_menu_kb(),
            parse_mode="md",
        )
        return

    if data == "resume":
        state["paused_until"] = None
        save_state()
        await event.edit("▶️ Автоответы включены\n\n" + status_text(), buttons=main_menu_kb())
        return

    if data == "status" or data == "back":
        await event.edit(status_text(), buttons=main_menu_kb())
        return

    if data == "bl:menu":
        await event.edit(
            f"🚫 **Blacklist** ({len(state['blacklist'])} чел.)\n"
            "Люди, кому бот НЕ отвечает вообще.",
            buttons=list_menu_kb("bl"),
            parse_mode="md",
        )
        return

    if data == "vip:menu":
        await event.edit(
            f"⭐ **VIP** ({len(state['vip'])} чел.)\n"
            "Люди, чьи сообщения бот пересылает тебе без ответа.",
            buttons=list_menu_kb("vip"),
            parse_mode="md",
        )
        return

    if data in ("bl:add", "vip:add"):
        kind = "blacklist" if data.startswith("bl") else "vip"
        awaiting.clear()
        awaiting[kind] = True
        await event.edit(
            f"➕ Перешли сюда любое сообщение того, кого добавить в **{kind}**.\n"
            "Или нажми Отмена.",
            buttons=[[Button.inline("❌ Отмена", b"back")]],
            parse_mode="md",
        )
        return

    if data in ("bl:show", "vip:show"):
        kind = "blacklist" if data.startswith("bl") else "vip"
        ids = state[kind]
        if not ids:
            await event.edit(
                f"Список **{kind}** пуст.",
                buttons=list_menu_kb("bl" if kind == "blacklist" else "vip"),
                parse_mode="md",
            )
            return
        lines = []
        buttons = []
        for uid in ids:
            try:
                ent = await user_client.get_entity(uid)
                name = ent.first_name or "(без имени)"
            except Exception:
                name = f"id={uid}"
            lines.append(f"• {name} (`{uid}`)")
            prefix = "bl" if kind == "blacklist" else "vip"
            buttons.append(
                [Button.inline(f"❌ Убрать {name}", f"{prefix}:rm:{uid}".encode())]
            )
        buttons.append([Button.inline("◀️ Назад", b"back")])
        await event.edit(
            f"**{kind.capitalize()}**:\n" + "\n".join(lines),
            buttons=buttons,
            parse_mode="md",
        )
        return

    if data.startswith("bl:rm:") or data.startswith("vip:rm:"):
        kind = "blacklist" if data.startswith("bl") else "vip"
        uid = int(data.rsplit(":", 1)[1])
        if uid in state[kind]:
            state[kind].remove(uid)
            save_state()
        await event.answer("Убран")
        event.data = f"{'bl' if kind == 'blacklist' else 'vip'}:show".encode()
        await on_callback(event)
        return


async def main():
    global my_id

    await user_client.start()
    me = await user_client.get_me()
    my_id = me.id
    log.info(f"Userbot запущен: {me.first_name} (id={me.id})")

    await bot_client.start(bot_token=BOT_TOKEN)
    bot_me = await bot_client.get_me()
    log.info(f"Bot-помощник запущен: @{bot_me.username}")

    try:
        await bot_client.send_message(
            my_id,
            "🤖 **Автоответчик запущен**\n\n" + status_text(),
            buttons=main_menu_kb(),
            parse_mode="md",
        )
    except Exception:
        log.exception("Не смог написать админу — открой чат с ботом-помощником и напиши /start")

    await asyncio.gather(
        user_client.run_until_disconnected(),
        bot_client.run_until_disconnected(),
    )


if __name__ == "__main__":
    asyncio.run(main())
