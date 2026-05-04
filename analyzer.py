import json
import asyncio
from openai import OpenAI
from database import get_unanalyzed, mark_analyzed
from bot import send_message
from dotenv import load_dotenv
import os

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY"),
)

with open("FRK_ANALYZ_PROMT.txt", "r", encoding="utf-8") as f:
    PROMPT = f.read()

def parse_response(raw: str) -> dict:
    raw = raw.strip()

    # Чистим markdown обёртку
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    try:
        start = raw.index("{")
        end = raw.rindex("}") + 1
        return json.loads(raw[start:end])
    except (json.JSONDecodeError, ValueError):
        pass

    print(f"Не удалось распарсить JSON, сырой ответ:\n{raw}")
    return {
        "category": 4,
        "real_price_min": 0,
        "real_price_max": 0,
        "deadline_days": 0,
        "risks": ["Не удалось проанализировать заказ"],
        "summary": "Ошибка анализа",
        "tags": []
    }

def analyze_project(project: dict) -> dict:
    response = client.chat.completions.create(
        model="deepseek/deepseek-chat-v3-0324",
        messages=[
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": f"Заказ:\nНазвание: {project['title']}\nОписание: {project['description']}\nБюджет заказчика: {project['wanted_budget']} ₽"}
        ]
    )

    raw = response.choices[0].message.content
    return parse_response(raw)


async def run_analyzer():
    """Основной цикл анализа — берём все непроанализированные заказы из БД"""
    rows = get_unanalyzed()

    if not rows:
        print("Нет новых заказов для анализа")
        return

    print(f"Найдено непроанализированных заказов: {len(rows)}")

    for row in rows:
        project = {
            "id":           row[0],
            "title":        row[1],
            "description":  row[2],
            "link":         row[3],
            "wanted_budget": row[4],
            "max_budget":   row[5],
        }

        try:
            print(f"Анализирую: {project['title']}...")

            result = analyze_project(project)

            mark_analyzed(project["id"], result)

            if result["category"] in [1, 2]:
                await send_message(project, result)
                print(f"✅ Отправлен в Telegram: {project['title']} → категория {result['category']}")
            else:
                print(f"⏭️  Пропущен: {project['title']} → категория {result['category']}")

            await asyncio.sleep(3)

        except Exception as e:
            print(f"❌ Ошибка анализа [{project['title']}]: {e}")
            continue
