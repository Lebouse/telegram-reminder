# database.py
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
import logging

from config import DATABASE_PATH, TIMEZONE

logger = logging.getLogger(__name__)
_db_lock = threading.RLock()

def init_db():
    """Инициализирует базу данных и создаёт таблицы."""
    with get_db_connection() as conn:
        # Таблица для запланированных сообщений
        conn.execute('''
            CREATE TABLE IF NOT EXISTS scheduled_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_text TEXT,
                photo_file_id TEXT,
                caption TEXT,
                publish_at TEXT NOT NULL,
                recurrence TEXT NOT NULL DEFAULT 'once',
                pin BOOLEAN NOT NULL DEFAULT 0,
                notify BOOLEAN NOT NULL DEFAULT 1,
                delete_after_days INTEGER,
                active BOOLEAN NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                last_published_at TEXT,
                max_end_date TEXT
            )
        ''')
        
        # Таблица для архива выполненных публикаций
        conn.execute('''
            CREATE TABLE IF NOT EXISTS published_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scheduled_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                published_at TEXT NOT NULL DEFAULT (datetime('now')),
                content TEXT,
                photo_file_id TEXT,
                status TEXT NOT NULL DEFAULT 'published'
            )
        ''')
        
        # Таблица доверенных чатов (куда добавлен бот)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS trusted_chats (
                chat_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                added_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        ''')
        
        conn.commit()
        logger.info("✅ База данных инициализирована")

@contextmanager
def get_db_connection():
    """Потокобезопасное подключение к SQLite."""
    with _db_lock:
        conn = sqlite3.connect(
            DATABASE_PATH,
            check_same_thread=False,
            timeout=20
        )
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

def add_scheduled_message(data):
    """Добавляет новое запланированное сообщение."""
    with get_db_connection() as conn:
        # Устанавливаем максимальный срок публикации - 1 год
        max_end_date = (datetime.now(TIMEZONE).replace(tzinfo=None) + timedelta(days=365)).isoformat()
        
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO scheduled_messages (
                chat_id, message_text, photo_file_id, caption,
                publish_at, recurrence, pin, notify,
                delete_after_days, max_end_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            data['chat_id'],
            data.get('text'),
            data.get('photo_file_id'),
            data.get('caption'),
            data['publish_at'],
            data['recurrence'],
            int(data.get('pin', False)),
            int(data.get('notify', True)),
            data.get('delete_after_days'),
            max_end_date
        ))
        
        msg_id = cursor.lastrowid
        conn.commit()
        logger.info(f"✅ Добавлена задача ID={msg_id} для чата {data['chat_id']}")
        return msg_id

def get_all_active_messages():
    """Возвращает все активные запланированные сообщения."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM scheduled_messages
            WHERE active = 1 AND publish_at <= ?
            ORDER BY publish_at ASC
        ''', (datetime.utcnow().isoformat(),))
        return cursor.fetchall()

def add_trusted_chat(chat_id, title):
    """Добавляет чат в список доверенных."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO trusted_chats (chat_id, title)
            VALUES (?, ?)
        ''', (chat_id, title))
        conn.commit()
        logger.info(f"✅ Чат {chat_id} ({title}) добавлен в доверенные")

def get_trusted_chats():
    """Возвращает список доверенных чатов."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM trusted_chats ORDER BY title')
        return cursor.fetchall()

def archive_published_message(scheduled_id, chat_id, message_id, content, photo_file_id):
    """Архивирует опубликованное сообщение."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO published_messages (
                scheduled_id, chat_id, message_id, content, photo_file_id
            ) VALUES (?, ?, ?, ?, ?)
        ''', (scheduled_id, chat_id, message_id, content, photo_file_id))
        conn.commit()
        logger.debug(f"✅ Архивировано сообщение ID={message_id} из задачи {scheduled_id}")

def deactivate_message(msg_id):
    """Деактивирует сообщение (логическое удаление)."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE scheduled_messages SET active = 0 WHERE id = ?
        ''', (msg_id,))
        conn.commit()
        logger.info(f"⏹️ Задача {msg_id} деактивирована")
        return cursor.rowcount > 0
