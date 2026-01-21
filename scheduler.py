# scheduler.py
import logging
import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from pytz import utc

from config import TIMEZONE
from database import get_all_active_messages
from bot import publish_and_reschedule

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone=TIMEZONE)

def schedule_all_jobs(application):
    """Планирует все активные задачи при запуске."""
    messages = get_all_active_messages()
    for msg in messages:
        if not msg['active']:
            continue
            
        publish_at = datetime.datetime.fromisoformat(msg['publish_at'])
        now = datetime.datetime.now(utc).replace(tzinfo=None)
        
        # Если время публикации в прошлом - публикуем немедленно
        if publish_at <= now:
            logger.info(f"⏰ Немедленная публикация задачи {msg['id']} (время в прошлом)")
            application.create_task(publish_and_reschedule(msg['id'], application))
        else:
            # Планируем задачу на указанное время
            job_id = f"publish_{msg['id']}"
            scheduler.add_job(
                publish_and_reschedule,
                trigger=DateTrigger(run_date=publish_at.replace(tzinfo=utc)),
                args=[msg['id'], application],
                id=job_id,
                misfire_grace_time=3600,  # 1 час на обработку
                replace_existing=True
            )
            logger.info(f"✅ Запланирована публикация задачи {msg['id']} на {publish_at}")

async def start_scheduler(application):
    """Запускает планировщик задач."""
    scheduler.start()
    logger.info("✅ Планировщик задач запущен")
    schedule_all_jobs(application)
