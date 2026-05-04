import asyncio
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from database import init_db, delete_old_projects
from parser import get_projects, save_project
from analyzer import run_analyzer

async def job_parse():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔄 Запуск парсинга...")
    try:
        projects = await get_projects()
        for p in projects:
            save_project(p)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ Парсинг завершён. Заказов: {len(projects)}")
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ Ошибка парсинга: {e}")

async def job_analyze():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🧠 Запуск анализа...")
    try:
        await run_analyzer()
        print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ Анализ завершён")
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ Ошибка анализа: {e}")

async def job_cleanup():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🗑 Удаление старых заказов...")
    try:
        delete_old_projects()
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ Ошибка очистки: {e}")

async def main():
    init_db()

    scheduler = AsyncIOScheduler()

    # Парсинг каждые 30 минут
    scheduler.add_job(
    job_parse,
    "interval",
    minutes=30,
    misfire_grace_time=300,
    coalesce=True
)

    scheduler.add_job(
        job_analyze,
        "interval",
        minutes=30,
        misfire_grace_time=300, 
        coalesce=True,
        start_date=datetime.now() + timedelta(minutes=5)
    )

    # Очистка каждый день в 03:00
    scheduler.add_job(job_cleanup, "cron", hour=3, minute=0)

    scheduler.start()
    print("✅ Scheduler запущен")

    await job_parse()
    await job_analyze()

    while True:
        await asyncio.sleep(60)

asyncio.run(main())
