"""
Таймер-бот с автоматическим обновлением списков после истечения
Python 3.11+, python-telegram-bot 20.x
"""

import os
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List

import pathlib
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

load_dotenv()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.WARNING,
)

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
GROUP_ID: int = -1002557055751
ADMIN_ID: int = 5948759106
WELCOME_IMAGE: str = str(pathlib.Path(__file__).with_name("welcome.jpg"))

SERVER, PARKING, TIME = range(3)
timers: Dict[int, List[Dict[str, Any]]] = {}
running_loops: Dict[int, asyncio.Task] = {}

# ------------------------------------------------------------------
# Утилиты
# ------------------------------------------------------------------
def fmt(td: timedelta) -> str:
    total = int(td.total_seconds())
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def build_text(chat_id: int) -> str:
    if chat_id not in timers or not timers[chat_id]:
        return "❌Нет активных таймеров."
    lines = []
    for idx, t in enumerate(timers[chat_id], 1):
        left = t["end"] - datetime.utcnow()
        if left.total_seconds() <= 0:
            continue
        lines.append(
            f"{idx}. 🔸Сервер {t['server']}, парковка {t['parking']}: {fmt(left)}"
        )
    return "\n".join(lines) if lines else "❌Нет активных таймеров."

main_kb = ReplyKeyboardMarkup(
    [["➕Добавить таймер", "📤Отправить список"]],
    resize_keyboard=True,
)

# ------------------------------------------------------------------
# Conversation
# ------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    if not context.user_data.get("seen_welcome"):
        text = (
            "👋 *Привет, я таймер-бот!*\n\n"
            "🔹 Нажми *«Добавить таймер»* и введи:\n"
            "1️⃣ Номер сервера\n"
            "2️⃣ Номер парковки\n"
            "3️⃣ Время в формате `ЧЧ:ММ:СС`\n\n"
            "🔹 После добавления нажми *«Обновить»* в списке, чтобы увидеть актуальное время.\n\n"
            "🔹 Когда внесёшь все таймеры — нажми *«Отправить список»*.\n"
            "  Список отправится и в группу *«Темная лига»*, и вам в личку."
        )
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=WELCOME_IMAGE,
            caption=text,
            parse_mode="Markdown",
            reply_markup=main_kb,
        )
        context.user_data["seen_welcome"] = True
        return ConversationHandler.END
    await update.message.reply_text("Введите номер сервера:", reply_markup=main_kb)
    return SERVER

async def get_server(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["server"] = update.message.text
    await update.message.reply_text("Введите номер парковки:", reply_markup=main_kb)
    return PARKING

async def get_parking(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["parking"] = update.message.text
    await update.message.reply_text("Введите время в формате ЧЧ:ММ:СС:", reply_markup=main_kb)
    return TIME

async def get_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        h, m, s = map(int, update.message.text.split(":"))
        delta = timedelta(hours=h, minutes=m, seconds=s)
    except Exception:
        await update.message.reply_text(
            "Неверный формат. Введите ЧЧ:ММ:СС:", reply_markup=main_kb
        )
        return TIME

    server = context.user_data["server"]
    parking = context.user_data["parking"]
    end = datetime.utcnow() + delta
    chat_id = update.effective_chat.id

    timers.setdefault(chat_id, [])
    timers[chat_id].append({"end": end, "server": server, "parking": parking, "warned": False})

    if chat_id not in running_loops or running_loops[chat_id].done():
        running_loops[chat_id] = context.application.create_task(timer_loop(chat_id, context))

    msg = await update.message.reply_text(
        build_text(chat_id),
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔄Обновить", callback_data=f"upd_chat_{chat_id}")]]
        ),
    )
    context.chat_data["msg_id"] = msg.message_id
    await update.message.reply_text("Таймер сохранён ✅", reply_markup=main_kb)
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Операция отменена.", reply_markup=main_kb)
    return ConversationHandler.END

# ------------------------------------------------------------------
# Callback «Добавить таймер» (перезапуск диалога)
# ------------------------------------------------------------------
async def add_more(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Введите номер сервера:", reply_markup=main_kb)
    return SERVER

# ------------------------------------------------------------------
# Отправить список в группу и личку
# ------------------------------------------------------------------
async def send_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if chat_id not in timers or not timers[chat_id]:
        await update.message.reply_text("❌ Нет активных таймеров", reply_markup=main_kb)
        return

    text_to_send = build_text(chat_id)

    # группа
    old_gid = context.bot_data.get("group_msg_id", {}).get(chat_id)
    if old_gid:
        try:
            await context.bot.edit_message_text(
                "❌ Актуальный список перемещён вниз",
                chat_id=GROUP_ID,
                message_id=old_gid,
            )
        except Exception:
            pass
    try:
        g_msg = await context.bot.send_message(
            GROUP_ID,
            text_to_send,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔄Обновить", callback_data=f"upd_group_{chat_id}")]]
            ),
        )
        context.bot_data.setdefault("group_msg_id", {})[chat_id] = g_msg.message_id
    except Exception as e:
        logging.warning("Не удалось отправить список в группу: %s", e)

    # личка админа
    old_aid = context.bot_data.get("admin_msg_id", {}).get(chat_id)
    if old_aid:
        try:
            await context.bot.edit_message_text(
                "❌ Актуальный список перемещён вниз",
                chat_id=ADMIN_ID,
                message_id=old_aid,
            )
        except Exception:
            pass
    try:
        a_msg = await context.bot.send_message(
            ADMIN_ID,
            text_to_send,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔄Обновить", callback_data=f"upd_admin_{chat_id}")]]
            ),
        )
        context.bot_data.setdefault("admin_msg_id", {})[chat_id] = a_msg.message_id
    except Exception as e:
        logging.warning("Не удалось отправить список админу: %s", e)

    await update.message.reply_text("📤 Список отправлен в группу и админу в личку", reply_markup=main_kb)

# ------------------------------------------------------------------
# Обработчик кнопки «Обновить» для чата, группы и админа
# ------------------------------------------------------------------
async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        _prefix, _place, chat_id_str = query.data.split("_", 2)
    except ValueError:
        await query.answer("Неверный формат callback")
        return

    chat_id = int(chat_id_str)
    target_chat = {
        "group": GROUP_ID,
        "admin": ADMIN_ID,
        "chat": chat_id,
    }.get(_place)

    if not target_chat or chat_id not in timers or not timers[chat_id]:
        await query.answer("❌ Нет активных таймеров")
        return

    try:
        await context.bot.edit_message_text(
            build_text(chat_id),
            chat_id=target_chat,
            message_id=query.message.message_id,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔄Обновить", callback_data=query.data)]]
            ),
        )
    except Exception as e:
        logging.warning("Не удалось обновить: %s", e)
    await query.answer()

# ------------------------------------------------------------------
# Фоновый цикл — только напоминания + авто-обновление списков
# ------------------------------------------------------------------
async def timer_loop(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    while True:
        if chat_id not in timers:
            break

        now = datetime.utcnow()
        new_list = []
        changed = False  # флаг: были ли изменения

        for t in timers[chat_id]:
            left = t["end"] - now
            left_seconds = int(left.total_seconds())

            if left_seconds <= 0:
                changed = True
                continue  # удаляем истёкшие

            if left_seconds <= 120 and not t.get("warned"):
                text = (
                    f"⚠️ Общий сбор! Сервер {t['server']}, парковка {t['parking']} – "
                    f"осталось 2 минуты!\n"
                    f"@Evgeny_404"
                )
                try:
                    await context.bot.send_message(chat_id, text)
                    await context.bot.send_message(GROUP_ID, text)
                    await context.bot.send_message(ADMIN_ID, text)
                except Exception as e:
                    logging.warning("Не удалось отправить напоминание: %s", e)
                t["warned"] = True

            new_list.append(t)

        if changed or (new_list != timers[chat_id]):
            timers[chat_id] = new_list
            await update_all_lists(chat_id, context)

        await asyncio.sleep(5)

# ------------------------------------------------------------------
# Обновление всех списков после изменения
# ------------------------------------------------------------------
async def update_all_lists(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    text = build_text(chat_id)

    # 1. Обновляем личный чат
    msg_id = context.chat_data.get("msg_id")
    if msg_id:
        try:
            await context.bot.edit_message_text(
                text,
                chat_id=chat_id,
                message_id=msg_id,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔄Обновить", callback_data=f"upd_chat_{chat_id}")]]
                ),
            )
        except Exception:
            pass

    # 2. Обновляем группу
    g_id = context.bot_data.get("group_msg_id", {}).get(chat_id)
    if g_id:
        try:
            await context.bot.edit_message_text(
                text,
                chat_id=GROUP_ID,
                message_id=g_id,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔄Обновить", callback_data=f"upd_group_{chat_id}")]]
                ),
            )
        except Exception:
            pass

    # 3. Обновляем личку админа
    a_id = context.bot_data.get("admin_msg_id", {}).get(chat_id)
    if a_id:
        try:
            await context.bot.edit_message_text(
                text,
                chat_id=ADMIN_ID,
                message_id=a_id,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔄Обновить", callback_data=f"upd_admin_{chat_id}")]]
                ),
            )
        except Exception:
            pass

# ------------------------------------------------------------------
# Главная функция
# ------------------------------------------------------------------
def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(add_more, pattern="^add_more$"),
            MessageHandler(filters.Regex("^➕Добавить таймер$"), start),
        ],
        states={
            SERVER: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_server)],
            PARKING: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_parking)],
            TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_time)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.Regex("^📤Отправить список$"), send_list))
    app.add_handler(CallbackQueryHandler(refresh, pattern=r"^upd_"))
    app.run_polling()

if __name__ == "__main__":
    main()