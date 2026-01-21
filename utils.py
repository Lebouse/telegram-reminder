# utils.py
import datetime
import re
from typing import Optional
from pytz import timezone, utc

from config import TIMEZONE

def parse_user_datetime(input_str: str) -> tuple[datetime.datetime, datetime.datetime]:
    """
    Парсит дату и время из строки в формате "ДД.ММ.ГГГГ ЧЧ:ММ".
    Возвращает кортеж (локальное_время, utc_время).
    """
    input_str = input_str.strip()
    
    # Проверяем формат
    if not re.match(r'^\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}$', input_str):
        raise ValueError('Неверный формат даты. Используйте: ДД.ММ.ГГГГ ЧЧ:ММ')
    
    try:
        # Парсим локальное время
        naive_local = datetime.datetime.strptime(input_str, '%d.%m.%Y %H:%M')
        
        # Локализуем в часовом поясе пользователя
        local_tz = TIMEZONE
        local_time = local_tz.localize(naive_local)
        
        # Конвертируем в UTC
        utc_time = local_time.astimezone(utc)
        
        return naive_local, utc_time
        
    except Exception as e:
        raise ValueError(f'Ошибка парсинга даты: {e}')

def next_recurrence_time(original: datetime.datetime, recurrence: str, last: datetime.datetime) -> Optional[datetime.datetime]:
    """
    Вычисляет следующее время публикации на основе периода повторения.
    
    Args:
        original: Исходное время первой публикации
        recurrence: Период повторения ('once', 'daily', 'weekly', 'monthly')
        last: Время последней публикации
    
    Returns:
        Следующее время публикации или None если повторение завершено
    """
    if recurrence == 'once':
        return None
    
    now = datetime.datetime.utcnow()
    
    try:
        if recurrence == 'daily':
            return last + datetime.timedelta(days=1)
        
        elif recurrence == 'weekly':
            return last + datetime.timedelta(weeks=1)
        
        elif recurrence == 'monthly':
            # Прибавляем один месяц
            month = last.month % 12 + 1
            year = last.year + (1 if last.month == 12 else 0)
            day = min(last.day, 28)  # Максимум 28 дней для февраля
            return datetime.datetime(year, month, day, last.hour, last.minute, tzinfo=utc)
    
    except Exception as e:
        logger.warning(f"Ошибка расчёта следующего времени: {e}")
    
    return None

def escape_markdown_v2(text: str) -> str:
    """Экранирует спецсимволы для MarkdownV2."""
    if not text:
        return ""
    
    # Сначала экранируем обратные слеши
    text = text.replace('\\', '\\\\')
    
    # Затем другие спецсимволы
    for char in '_*[]()~`>#+-=|{}.!':
        text = text.replace(char, '\\' + char)
    
    return text

def detect_media_type(file_id: str) -> Optional[str]:
    """Определяет тип медиа по file_id (упрощённая версия)."""
    if not file_id:
        return None
    
    # В реальной реализации нужно получать информацию о файле через API
    if file_id.startswith('AgAC') or file_id.startswith('AAMC'):
        return 'photo'
    elif file_id.startswith('BQAC') or file_id.startswith('BAMC'):
        return 'document'
    
    return None
