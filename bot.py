# bot.py
# Telegram бот для планирования публикаций
# Версия: 1.0.0
# Автор: Lebouse

import logging
import datetime
import re
import asyncio
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ChatMember
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes,
    ConversationHandler, filters, CallbackQueryHandler, ChatMemberHandler
)
from telegram.constants import ChatType, ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from pytz import utc

from config import BOT_TOKEN, AUTHORIZED_USER_IDS, TIMEZONE
from database import (
    init_db, add_scheduled_message, get_all_active_messages,
    deactivate_message, add_trusted_chat, get_trusted_chats,
    archive_published_message, get_message_by_id, update_scheduled_message
)
from utils import (
    parse_user_datetime, next_recurrence_time,
    escape_markdown_v2, detect_media_type
)

# === Настройка логирования ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# === Константы состояний для ConversationHandler ===
(
    WAITING_CONTENT, SELECT_CHAT, INPUT_DATE, SELECT_RECURRENCE,
    SELECT_PIN, SELECT_NOTIFY, SELECT_DELETE_DAYS, EDIT_MESSAGE_ID,
    EDIT_CHAT, EDIT_CONTENT, EDIT_DATE, EDIT_RECURRENCE,
    EDIT_PIN, EDIT_NOTIFY, EDIT_DELETE_DAYS
) = range(16)

user_sessions = {}

# === Декоратор авторизации ===
def check_auth(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id not in AUTHORIZED_USER_IDS:
            await update.message.reply_text("❌ Доступ запрещён.")
            return
        return await func(update, context)
    return wrapper

# === Обработка добавления/удаления из чата ===
async def on_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Срабатывает при изменении статуса бота в чате."""
    my_chat_member = update.my_chat_member
    if not my_chat_member:
        return

    chat = my_chat_member.chat
    new_status = my_chat_member.new_chat_member.status
    old_status = my_chat_member.old_chat_member.status

    if new_status in ("member", "administrator"):
        if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            # Сохраняем чат в доверенные
            add_trusted_chat(chat.id, chat.title or f"Чат {chat.id}")
            logger.info(f"✅ Бот добавлен в чат {chat.id} ({chat.title})")
            
            # Отправляем сообщение админу
            for admin_id in AUTHORIZED_USER_IDS:
                try:
                    await context.bot.send_message(
                        admin_id,
                        f"✅ Бот добавлен в чат:\n"
                        f"ID: {chat.id}\n"
                        f"Название: {chat.title}\n"
                        f"Тип: {chat.type}",
                        parse_mode=ParseMode.HTML
                    )
                except Exception as e:
                    logger.warning(f"Не удалось отправить уведомление админу {admin_id}: {e}")
    
    elif new_status in ("left", "kicked"):
        logger.info(f"⏹️ Бот удалён из чата {chat.id}")

# === Команда /start ===
@check_auth
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начинает диалог планирования сообщения."""
    keyboard = [
        [InlineKeyboardButton("➕ Добавить публикацию", callback_data="add_publication")],
        [InlineKeyboardButton("📋 Мои публикации", callback_data="list_publications")],
        [InlineKeyboardButton("🔍 Доверенные чаты", callback_data="list_chats")],
        [InlineKeyboardButton("❓ Помощь", callback_data="help")]
    ]
    
    await update.message.reply_text(
        "👋 Привет! Я бот для планирования публикаций в Telegram.\n\n"
        "Выберите действие:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ConversationHandler.END

@check_auth
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатия кнопок."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if query.data == "add_publication":
        await query.edit_message_text("📤 Отправьте сообщение (текст, фото или PDF), которое нужно запланировать как публикацию.")
        user_sessions[user_id] = {'step': 'waiting_content'}
        return WAITING_CONTENT
    
    elif query.data == "list_publications":
        tasks = get_all_active_messages()
        if not tasks:
            await query.edit_message_text("📭 Нет активных публикаций.")
            return ConversationHandler.END
        
        text = "📋 Активные публикации:\n\n"
        for task in tasks:
            time_str = task['publish_at'][:16]
            content = task['message_text'] or task['caption'] or "Медиа"
            text += f"ID: {task['id']}\n"
            text += f"Чат: {task['chat_id']}\n"
            text += f"Публикация: {time_str}\n"
            text += f"Контент: {content[:30]}...\n"
            text += f"Повтор: {task['recurrence']}\n"
            text += f"Закрепить: {'✅' if task['pin'] else '❌'}\n"
            text += f"Удалить через: {task['delete_after_days'] or 'никогда'} дн.\n"
            text += "-" * 30 + "\n"
        
        keyboard = [
            [InlineKeyboardButton("➕ Добавить", callback_data="add_publication")],
            [InlineKeyboardButton("✏️ Редактировать", callback_data="edit_publication")],
            [InlineKeyboardButton("🗑️ Удалить", callback_data="delete_publication")],
            [InlineKeyboardButton("🔙 Назад", callback_data="main_menu")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return ConversationHandler.END
    
    elif query.data == "list_chats":
        chats = get_trusted_chats()
        if not chats:
            await query.edit_message_text("📭 Бот не добавлен ни в один чат. Сначала добавьте его в группу как администратора.")
            return ConversationHandler.END
        
        text = "🔍 Доверенные чаты:\n\n"
        for chat in chats:
            text += f"ID: {chat['chat_id']}\n"
            text += f"Название: {chat['title']}\n"
            text += f"Добавлен: {chat['added_at'][:16]}\n"
            text += "-" * 30 + "\n"
        
        keyboard = [
            [InlineKeyboardButton("➕ Добавить в чат", url=f"https://t.me/{context.bot.username}?startgroup=true")],
            [InlineKeyboardButton("🔙 Назад", callback_data="main_menu")]
        ]
        await query.edit_message_text(
            text + "\n\nЧтобы добавить бота в новый чат, нажмите кнопку ниже и выберите чат. Бот должен иметь права администратора с возможностью отправки сообщений и закрепления.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return ConversationHandler.END
    
    elif query.data == "help":
        help_text = """
🤖 <b>Помощь по боту</b>

<b>Основные команды:</b>
• /start - главное меню
• /cancel - отменить текущую операцию

<b>Возможности бота:</b>
✅ Планирование публикаций на заданное время
✅ Поддержка текста, фото и документов (PDF)
✅ Повторение публикаций: ежедневно, еженедельно, ежемесячно
✅ Закрепление сообщений после публикации
✅ Автоматическое удаление через N дней (1-3)
✅ Отправка без уведомления участников
✅ Архивирование выполненных публикаций

<b>Чтобы использовать бота:</b>
1. Добавьте бота в группу как администратора
2. Выполните команду /start в личных сообщениях с ботом
3. Следуйте инструкциям бота

<b>Важно:</b>
• Бот должен иметь права администратора в чате
• Максимальный срок публикаций - 1 год
• Для фото/PDF сначала отправьте их боту, чтобы получить file_id
        """
        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="main_menu")]]
        await query.edit_message_text(
            help_text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return ConversationHandler.END
    
    elif query.data == "main_menu":
        keyboard = [
            [InlineKeyboardButton("➕ Добавить публикацию", callback_data="add_publication")],
            [InlineKeyboardButton("📋 Мои публикации", callback_data="list_publications")],
            [InlineKeyboardButton("🔍 Доверенные чаты", callback_data="list_chats")],
            [InlineKeyboardButton("❓ Помощь", callback_data="help")]
        ]
        await query.edit_message_text(
            "👋 Главное меню\n\nВыберите действие:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return ConversationHandler.END
    
    elif query.data == "cancel":
        user_sessions.pop(user_id, None)
        await query.edit_message_text("❌ Операция отменена.")
        return ConversationHandler.END
    
    return ConversationHandler.END

# === Обработка контента от пользователя ===
@check_auth
async def receive_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получает контент для публикации от админа."""
    user_id = update.effective_user.id
    if user_id not in AUTHORIZED_USER_IDS:
        return
    
    session = user_sessions.get(user_id, {})
    
    if update.message.text:
        session['text'] = update.message.text
        session['media_type'] = 'text'
        
    elif update.message.photo:
        session['photo_file_id'] = update.message.photo[-1].file_id
        session['caption'] = update.message.caption
        session['media_type'] = 'photo'
        
    elif update.message.document:
        mime = update.message.document.mime_type
        if mime in ('application/pdf', 'image/jpeg', 'image/png'):
            session['document_file_id'] = update.message.document.file_id
            session['caption'] = update.message.caption
            session['media_type'] = 'document'
        else:
            await update.message.reply_text("❌ Поддерживаются только PDF и изображения.")
            return WAITING_CONTENT
    
    else:
        await update.message.reply_text("❌ Пожалуйста, отправьте текст, фото или PDF.")
        return WAITING_CONTENT
    
    user_sessions[user_id] = session
    
    # Загружаем доверенные чаты
    chats = get_trusted_chats()
    if not chats:
        await update.message.reply_text(
            "❌ Бот не добавлен ни в один чат. Сначала добавьте его в группу как администратора.\n"
            "Нажмите /start для возврата в главное меню."
        )
        return ConversationHandler.END
    
    # Формируем кнопки выбора чата
    keyboard = []
    for chat in chats:
        keyboard.append([InlineKeyboardButton(
            f"{chat['title']} (ID: {chat['chat_id']})",
            callback_data=f"chat_{chat['chat_id']}"
        )])
    
    keyboard.append([InlineKeyboardButton("🔙 Отмена", callback_data="cancel")])
    
    await update.message.reply_text(
        "🎯 Выберите чат для публикации:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return SELECT_CHAT

# === Выбор чата ===
async def select_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает выбор чата для публикации."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if query.data == "cancel":
        user_sessions.pop(user_id, None)
        await query.edit_message_text("❌ Операция отменена.")
        return ConversationHandler.END
    
    chat_id_str = query.data.split('_')[1]
    chat_id = int(chat_id_str)
    
    session = user_sessions.get(user_id, {})
    session['chat_id'] = chat_id
    user_sessions[user_id] = session
    
    await query.edit_message_text(
        "📅 Введите дату и время первой публикации (формат: ДД.ММ.ГГГГ ЧЧ:ММ):"
    )
    return INPUT_DATE

# === Ввод даты и времени ===
async def input_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает ввод даты и времени публикации."""
    user_id = update.effective_user.id
    text = update.message.text.strip()
    
    try:
        # Парсим дату в локальном времени
        naive_local, utc_naive = parse_user_datetime(text)
        
        # Проверяем максимальный срок (365 дней)
        max_allowed = datetime.datetime.utcnow() + datetime.timedelta(days=365)
        if utc_naive > max_allowed:
            await update.message.reply_text(
                "❌ Максимальный срок публикации — 1 год от сегодняшнего дня.\n"
                "Попробуйте снова:"
            )
            return INPUT_DATE
        
        # Проверяем, что дата в будущем
        if utc_naive <= datetime.datetime.utcnow():
            await update.message.reply_text(
                "❌ Дата должна быть в будущем!\n"
                "Попробуйте снова (формат: ДД.ММ.ГГГГ ЧЧ:ММ):"
            )
            return INPUT_DATE
        
        session = user_sessions.get(user_id, {})
        session['publish_at'] = utc_naive.isoformat()
        user_sessions[user_id] = session
        
        keyboard = [
            [InlineKeyboardButton("Один раз", callback_data="once")],
            [InlineKeyboardButton("Ежедневно", callback_data="daily")],
            [InlineKeyboardButton("Еженедельно", callback_data="weekly")],
            [InlineKeyboardButton("Ежемесячно", callback_data="monthly")],
            [InlineKeyboardButton("🔙 Отмена", callback_data="cancel")]
        ]
        
        await update.message.reply_text(
            "🔄 Выберите периодичность публикации:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return SELECT_RECURRENCE
        
    except ValueError as e:
        await update.message.reply_text(f"❌ {str(e)}\nПопробуйте снова (формат: ДД.ММ.ГГГГ ЧЧ:ММ):")
        return INPUT_DATE

# === Выбор периодичности ===
async def select_recurrence(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает выбор периодичности."""
    query = update.callback_query
    await query.answer()
    
    if query.data == "cancel":
        user_id = query.from_user.id
        user_sessions.pop(user_id, None)
        await query.edit_message_text("❌ Операция отменена.")
        return ConversationHandler.END
    
    user_id = query.from_user.id
    session = user_sessions.get(user_id, {})
    session['recurrence'] = query.data
    user_sessions[user_id] = session
    
    keyboard = [
        [InlineKeyboardButton("✅ Да", callback_data="pin_yes"), InlineKeyboardButton("❌ Нет", callback_data="pin_no")],
        [InlineKeyboardButton("🔙 Отмена", callback_data="cancel")]
    ]
    
    await query.edit_message_text(
        "📌 Закрепить сообщение после публикации?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return SELECT_PIN

# === Выбор закрепления ===
async def select_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает выбор закрепления сообщения."""
    query = update.callback_query
    await query.answer()
    
    if query.data == "cancel":
        user_id = query.from_user.id
        user_sessions.pop(user_id, None)
        await query.edit_message_text("❌ Операция отменена.")
        return ConversationHandler.END
    
    user_id = query.from_user.id
    session = user_sessions.get(user_id, {})
    session['pin'] = (query.data == "pin_yes")
    user_sessions[user_id] = session
    
    keyboard = [
        [InlineKeyboardButton("✅ Да", callback_data="notify_yes"), InlineKeyboardButton("❌ Нет", callback_data="notify_no")],
        [InlineKeyboardButton("🔙 Отмена", callback_data="cancel")]
    ]
    
    await query.edit_message_text(
        "🔔 Оповестить участников (отправить без уведомления)?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return SELECT_NOTIFY

# === Выбор уведомления ===
async def select_notify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает выбор отправки без уведомления."""
    query = update.callback_query
    await query.answer()
    
    if query.data == "cancel":
        user_id = query.from_user.id
        user_sessions.pop(user_id, None)
        await query.edit_message_text("❌ Операция отменена.")
        return ConversationHandler.END
    
    user_id = query.from_user.id
    session = user_sessions.get(user_id, {})
    session['notify'] = (query.data == "notify_yes")
    user_sessions[user_id] = session
    
    keyboard = [
        [InlineKeyboardButton("1 день", callback_data="delete_1")],
        [InlineKeyboardButton("2 дня", callback_data="delete_2")],
        [InlineKeyboardButton("3 дня", callback_data="delete_3")],
        [InlineKeyboardButton("Никогда", callback_data="delete_0")],
        [InlineKeyboardButton("🔙 Отмена", callback_data="cancel")]
    ]
    
    await query.edit_message_text(
        "🗑️ Удалить публикацию через:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return SELECT_DELETE_DAYS

# === Выбор удаления ===
async def select_delete_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает выбор времени удаления."""
    query = update.callback_query
    await query.answer()
    
    if query.data == "cancel":
        user_id = query.from_user.id
        user_sessions.pop(user_id, None)
        await query.edit_message_text("❌ Операция отменена.")
        return ConversationHandler.END
    
    user_id = query.from_user.id
    session = user_sessions.get(user_id, {})
    
    # Извлекаем количество дней из callback_data
    days_str = query.data.split('_')[1]
    days = int(days_str) if days_str != "0" else None
    
    session['delete_after_days'] = days
    user_sessions[user_id] = session
    
    # Сохраняем в базу данных
    try:
        msg_id = add_scheduled_message(session)
        
        # Форматируем время в локальном формате
        publish_time = datetime.datetime.fromisoformat(session['publish_at']).replace(tzinfo=datetime.timezone.utc).astimezone(TIMEZONE)
        time_str = publish_time.strftime("%d.%m.%Y %H:%M")
        
        await query.edit_message_text(
            f"✅ Публикация успешно запланирована!\n\n"
            f"📋 ID задачи: {msg_id}\n"
            f"💬 Чат: {session['chat_id']}\n"
            f"⏰ Время: {time_str}\n"
            f"🔄 Периодичность: {session['recurrence']}\n"
            f"📌 Закрепить: {'✅' if session['pin'] else '❌'}\n"
            f"🔕 Без уведомления: {'✅' if session['notify'] else '❌'}\n"
            f"🗑️ Удалить через: {days if days else 'никогда'} дней"
        )
        
        # Очищаем сессию
        user_sessions.pop(user_id, None)
        
    except Exception as e:
        logger.error(f"Ошибка при сохранении задачи: {e}")
        await query.edit_message_text(f"❌ Ошибка при сохранении: {str(e)}")
    
    return ConversationHandler.END

# === Отмена операции ===
@check_auth
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отменяет текущую операцию."""
    user_id = update.effective_user.id
    user_sessions.pop(user_id, None)
    await update.message.reply_text("❌ Операция отменена.")
    return ConversationHandler.END

# === Публикация и перепланирование ===
async def publish_and_reschedule(msg_id, application):
    """Публикует сообщение и перепланирует следующую публикацию."""
    from database import get_db_connection
    
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM scheduled_messages WHERE id = ?", (msg_id,))
            task = cursor.fetchone()
            
            if not task or not task['active']:
                logger.warning(f"Задача {msg_id} не найдена или деактивирована")
                return
            
            # Публикуем сообщение
            bot = application.bot
            content = task['message_text'] or task['caption'] or ""
            photo_file_id = task['photo_file_id']
            document_file_id = task['document_file_id']
            
            message = None
            if photo_file_id:
                message = await bot.send_photo(
                    chat_id=task['chat_id'],
                    photo=photo_file_id,
                    caption=escape_markdown_v2(content) if content else None,
                    parse_mode=ParseMode.MARKDOWN_V2,
                    disable_notification=not task['notify']
                )
            elif document_file_id:
                message = await bot.send_document(
                    chat_id=task['chat_id'],
                    document=document_file_id,
                    caption=escape_markdown_v2(content) if content else None,
                    parse_mode=ParseMode.MARKDOWN_V2,
                    disable_notification=not task['notify']
                )
            else:
                message = await bot.send_message(
                    chat_id=task['chat_id'],
                    text=escape_markdown_v2(content),
                    parse_mode=ParseMode.MARKDOWN_V2,
                    disable_notification=not task['notify']
                )
            
            if not message:
                logger.error(f"Не удалось опубликовать сообщение для задачи {msg_id}")
                return
            
            # Закрепляем если нужно
            if task['pin']:
                try:
                    await bot.pin_chat_message(
                        chat_id=task['chat_id'],
                        message_id=message.message_id,
                        disable_notification=True
                    )
                except Exception as e:
                    logger.warning(f"Не удалось закрепить сообщение: {e}")
            
            # Архивируем публикацию
            archive_published_message(
                scheduled_id=task['id'],
                chat_id=task['chat_id'],
                message_id=message.message_id,
                content=content,
                photo_file_id=photo_file_id,
                document_file_id=document_file_id
            )
            
            logger.info(f"✅ Опубликовано сообщение ID={message.message_id} для задачи {msg_id}")
            
            # Планируем удаление если нужно
            if task['delete_after_days']:
                deletion_time = datetime.datetime.utcnow() + datetime.timedelta(days=task['delete_after_days'])
                schedule_message_deletion(application, task['chat_id'], message.message_id, deletion_time)
                logger.info(f"⏰ Запланировано удаление сообщения {message.message_id} через {task['delete_after_days']} дней")
            
            # Перепланируем следующую публикацию для повторяющихся задач
            if task['recurrence'] != 'once':
                next_time = next_recurrence_time(
                    original=datetime.datetime.fromisoformat(task['original_publish_at']),
                    recurrence=task['recurrence'],
                    last=datetime.datetime.fromisoformat(task['publish_at'])
                )
                
                if next_time:
                    # Проверяем максимальный срок (365 дней)
                    max_end_date = datetime.datetime.fromisoformat(task['max_end_date'])
                    if next_time > max_end_date:
                        logger.info(f"⏹️ Задача {msg_id} достигла максимального срока. Деактивируем.")
                        deactivate_message(msg_id)
                        return
                    
                    # Обновляем время следующей публикации
                    cursor.execute('''
                        UPDATE scheduled_messages 
                        SET publish_at = ?, last_published_at = ? 
                        WHERE id = ?
                    ''', (next_time.isoformat(), datetime.datetime.utcnow().isoformat(), msg_id))
                    conn.commit()
                    
                    logger.info(f"⏰ Задача {msg_id} перепланирована на {next_time}")
            
    except Exception as e:
        logger.error(f"Ошибка при публикации задачи {msg_id}: {e}", exc_info=True)

async def delete_message(application, chat_id, message_id):
    """Удаляет сообщение из чата."""
    try:
        bot = application.bot
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        logger.info(f"🗑️ Сообщение {message_id} удалено из чата {chat_id}")
    except Exception as e:
        logger.warning(f"Не удалось удалить сообщение {message_id}: {e}")

def schedule_message_deletion(application, chat_id, message_id, deletion_time):
    """Запланировать удаление сообщения."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        delete_message,
        'date',
        run_date=deletion_time,
        args=[application, chat_id, message_id],
        id=f"delete_{message_id}",
        misfire_grace_time=3600
    )
    scheduler.start()

# === Основная функция ===
async def main():
    """Основная функция запуска бота."""
    # Инициализируем базу данных
    init_db()
    
    # Создаём приложение
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Регистрируем обработчики
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAITING_CONTENT: [MessageHandler(filters.ALL & ~filters.COMMAND, receive_content)],
            SELECT_CHAT: [CallbackQueryHandler(select_chat)],
            INPUT_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, input_date)],
            SELECT_RECURRENCE: [CallbackQueryHandler(select_recurrence)],
            SELECT_PIN: [CallbackQueryHandler(select_pin)],
            SELECT_NOTIFY: [CallbackQueryHandler(select_notify)],
            SELECT_DELETE_DAYS: [CallbackQueryHandler(select_delete_days)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    
    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(ChatMemberHandler(on_chat_member_update, ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_handler(CommandHandler("cancel", cancel))
    
    # ИМПОРТИРУЕМ start_scheduler только внутри функции, где он нужен
    from scheduler import start_scheduler
    # Запускаем планировщик
    await start_scheduler(application)
    
    # Запускаем бота
    logger.info("✅ Бот запущен и готов к работе!")
    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)
    
    # Запускаем бесконечный цикл
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
