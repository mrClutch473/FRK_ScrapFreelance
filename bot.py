import asyncio
import sqlite3
from aiogram import Bot, Dispatcher
from aiogram.types import Message, CallbackQuery
from openai import OpenAI
import json

from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from database import search_by_keyword, search_by_filters, get_top_tags
from datetime import datetime

from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from database import get_projects_by_category

from dotenv import load_dotenv
import os

load_dotenv()

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY"),
)

TOKEN = os.getenv("TELEGRAM_TOKEN")
MY_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))

with open("FRK_ANALYZ_PROMT.txt", "r", encoding="utf-8") as f:
    PROMPT = f.read()

with open("FRK_RESPONSE_PROMT.txt", "r", encoding="utf-8") as f:
    RESPONSE_PROMPT = f.read()

dp = Dispatcher()

class ViewState(StatesGroup):
    browsing = State()

class SearchState(StatesGroup):
    waiting_keyword = State()      
    filter_category = State()      
    filter_tag = State()           
    filter_budget = State()         
    browsing_results = State()      

# ─────────────────────────────────────────────
# Вспомогательная функция: получить заказ по ID
# ─────────────────────────────────────────────

def get_project_by_id(project_id: int) -> dict | None:
    """Получить полные данные заказа из БД по ID"""
    conn = sqlite3.connect("projects.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, title, description, link, wanted_budget, max_budget,
               real_price_min, real_price_max, deadline_days, risks, summary, tags
        FROM projects
        WHERE id = ?
    """, (project_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return None

    return {
        "id":             row[0],
        "title":          row[1],
        "description":    row[2],
        "link":           row[3],
        "wanted_budget":  row[4],
        "max_budget":     row[5],
        "real_price_min": row[6],
        "real_price_max": row[7],
        "deadline_days":  row[8],
        "risks":          json.loads(row[9]) if row[9] else [],
        "summary":        row[10],
        "tags":           json.loads(row[11]) if row[11] else [],
    }

# ─────────────────────────────────────────────
# Генерация отклика через Claude Sonnet
# ─────────────────────────────────────────────

def generate_response_text(project: dict) -> dict:
    """Отправляем заказ в Claude Sonnet и получаем готовый отклик"""
    risks_text = "\n".join(f"- {r}" for r in project["risks"]) if project["risks"] else "Не выявлены"

    user_message = (
        f"Название заказа: {project['title']}\n\n"
        f"Описание заказа:\n{project['description']}\n\n"
        f"Желаемый бюджет заказчика: {project['wanted_budget']} ₽\n"
        f"Реальная стоимость выполнения: {project['real_price_min']} — {project['real_price_max']} ₽\n"
        f"Реальный срок выполнения: {project['deadline_days']} дней\n\n"
        f"Выявленные риски:\n{risks_text}"
    )

    response = client.chat.completions.create(
        model="anthropic/claude-sonnet-4",  
        messages=[
            {"role": "system", "content": RESPONSE_PROMPT},
            {"role": "user",   "content": user_message}
        ]
    )

    raw = response.choices[0].message.content.strip()

    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        try:
            start = raw.index("{")
            end = raw.rindex("}") + 1
            return json.loads(raw[start:end])
        except Exception:
            return {
                "title": project["title"],
                "response": raw,
                "price": project["real_price_min"],
                "deadline_days": project["deadline_days"],
            }

# ─────────────────────────────────────────────
# Клавиатуры
# ─────────────────────────────────────────────

def get_nav_keyboard(index: int, total: int, category: int, project_id: int) -> InlineKeyboardMarkup:
    """Кнопки навигации + кнопка генерации отклика"""
    buttons = []

    # Навигация
    nav_row = []
    if index > 0:
        nav_row.append(InlineKeyboardButton(text="◀️ Пред.", callback_data="proj_prev"))
    if index < total - 1:
        nav_row.append(InlineKeyboardButton(text="След. ▶️", callback_data="proj_next"))
    if nav_row:
        buttons.append(nav_row)

    # Кнопка генерации отклика
    buttons.append([
        InlineKeyboardButton(
            text="✍️ Сгенерировать отклик ✅",
            callback_data=f"gen_resp_{project_id}"
        )
    ])

    # Служебные кнопки
    buttons.append([
        InlineKeyboardButton(text="📂 Сменить категорию", callback_data="view_categories"),
        InlineKeyboardButton(text="🏠 Меню", callback_data="back_to_menu"),
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Сводка", callback_data="stats"),
            InlineKeyboardButton(text="📂 Заказы по категориям", callback_data="view_categories"),
        ],
        [
            InlineKeyboardButton(text="🔍 Обработать заказ", callback_data="process_order"),
            InlineKeyboardButton(text="✍️ Сгенерировать отклик", callback_data="generate_response"),
        ],
        [
            InlineKeyboardButton(text="🔎 Поиск заказов", callback_data="search_orders"),
        ]
    ])

# ─────────────────────────────────────────────
# Форматирование карточки заказа
# ─────────────────────────────────────────────

def format_project(row, index: int, total: int) -> str:
    risks = json.loads(row[8]) if row[8] else []
    risks_text = "\n".join(f"• {r}" for r in risks) if risks else "Не выявлены"

    tags = json.loads(row[12]) if row[12] else []
    tags_text = " ".join(f"#{t.replace(' ', '_')}" for t in tags) if tags else "—"

    return (
        f"📌 <b>{row[1]}</b>\n"
        f"🔗 {row[2]}\n\n"
        f"💰 Бюджет заказчика: {row[3]} - {row[4]} ₽\n"
        f"📊 Реальная стоимость: {row[5]} - {row[6]} ₽\n"
        f"⏱ Сроки: {row[7]} дней\n"
        f"📋 Откликов: {row[10]}\n"
        f"👤 Процент найма: {row[11]}%\n\n"
        f"🏷 {tags_text}\n\n"
        f"⚠️ Риски:\n{risks_text}\n\n"
        f"📝 {row[9]}\n\n"
        f"<i>Заказ {index + 1} из {total}</i>"
    )

# ─────────────────────────────────────────────
# Отправка нового заказа в Telegram
# ─────────────────────────────────────────────

async def send_message(project, res_raw):
    bot = Bot(token=TOKEN)
    try:
        result = res_raw

        risks = result.get('risks', [])
        risks_text = "\n".join(f"• {r}" for r in risks) if risks else "Не выявлены"

        tags = result.get('tags', [])
        tags_text = " ".join(f"#{t.replace(' ', '_')}" for t in tags) if tags else "—"

        text = (
            f"📌 <b>{project['title']}</b>\n"
            f"🔗 {project['link']}\n\n"
            f"💰 Бюджет заказчика: {project['wanted_budget']} — {project['max_budget']} ₽\n"
            f"📊 Реальная стоимость: {result['real_price_min']} — {result['real_price_max']} ₽\n"
            f"⏱ Сроки: {result['deadline_days']} дней\n"
            f"🏷 Категория: {result['category']}\n\n"
            f"🔖 {tags_text}\n\n"
            f"⚠️ Риски:\n{risks_text}\n\n"
            f"📝 {result['summary']}"
        )

        # Кнопка генерации отклика под каждым уведомлением
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✍️ Сгенерировать отклик ✅",
                    callback_data=f"gen_resp_{project['id']}"
                )
            ]
        ])

        await bot.send_message(
            chat_id=MY_CHAT_ID,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard
        )

    except Exception as e:
        print(f"Ошибка send_message: {e}")
        await bot.send_message(chat_id=MY_CHAT_ID, text=f"❌ Ошибка анализа: {e}")
    finally:
        await bot.session.close()

# ─────────────────────────────────────────────
# Обработчики навигации по заказам
# ─────────────────────────────────────────────

@dp.callback_query(lambda c: c.data.startswith("cat_"))
async def cb_open_category(callback: CallbackQuery, state: FSMContext):
    category = int(callback.data.split("_")[1])
    projects = get_projects_by_category(category)

    if not projects:
        await callback.message.edit_text(
            f"😔 <b>В категории {category} заказов нет</b>\n\nПопробуй другую категорию 👇",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📂 Выбрать категорию", callback_data="view_categories")],
                [InlineKeyboardButton(text="🏠 Меню", callback_data="back_to_menu")],
            ])
        )
        await callback.answer()
        return

    await state.set_state(ViewState.browsing)
    await state.update_data(category=category, index=0, projects=projects)

    project_id = projects[0][0]
    text = format_project(projects[0], 0, len(projects))
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=get_nav_keyboard(0, len(projects), category, project_id)
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data in ["proj_prev", "proj_next"])
async def cb_navigate(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    projects = data["projects"]
    index = data["index"]
    category = data["category"]

    if callback.data == "proj_next":
        index += 1
    else:
        index -= 1

    index = max(0, min(index, len(projects) - 1))
    await state.update_data(index=index)

    project_id = projects[index][0]
    text = format_project(projects[index], index, len(projects))
    try:
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=get_nav_keyboard(index, len(projects), category, project_id)
        )
    except Exception:
        pass

    await callback.answer()

# ─────────────────────────────────────────────
# Генерация отклика по ID заказа
# ─────────────────────────────────────────────

@dp.callback_query(lambda c: c.data.startswith("gen_resp_"))
async def cb_gen_resp(callback: CallbackQuery):
    project_id = int(callback.data.split("gen_resp_")[1])

    await callback.answer("⏳ Генерирую отклик, подожди...")
    await callback.message.reply("⏳ <b>Генерирую отклик...</b> Обычно занимает 5–15 секунд.", parse_mode="HTML")

    project = get_project_by_id(project_id)
    if not project:
        await callback.message.reply("❌ Заказ не найден в базе.")
        return

    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, generate_response_text, project)
    except Exception as e:
        await callback.message.reply(f"❌ Ошибка генерации: {e}")
        return

    response_text = (
        result.get("response", "")
        .replace("\\n\\n", "\n\n")
        .replace("—", "-")
    )
    price = result.get("price", "-")
    deadline = result.get("deadline_days", "-")

    text = (
        f"✍️ <b>Отклик для заказа:</b>\n"
        f"<i>{project['title']}</i>\n"
        f"🔗 {project['link']}\n\n"
        f"💬 <b>Текст отклика:</b>\n"
        f"{response_text}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Предлагаемая цена: <b>{price} ₽</b>\n"
        f"⏱ Предлагаемый срок: <b>{deadline} дней</b>"
    )

    await callback.message.reply(text, parse_mode="HTML")

# ─────────────────────────────────────────────
# Команды и меню
# ─────────────────────────────────────────────

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    stats = get_stats()
    cats = stats["by_category"]
    top_tags = get_top_tags(5)

    if top_tags:
        tags_text = "\n".join(f"   #{tag.replace(' ', '_')} — {count} заказов" for tag, count in top_tags)
    else:
        tags_text = "   Пока нет данных"

    text = (
        f"📊 <b>Сводка по заказам</b>\n\n"
        f"📦 Всего в базе: {stats['total']}\n\n"
        f"🏆 Категория 1 (Отличные): {cats.get(1, 0)}\n"
        f"👍 Категория 2 (Неплохие): {cats.get(2, 0)}\n"
        f"😐 Категория 3 (Средние): {cats.get(3, 0)}\n"
        f"👎 Категория 4 (Слабые): {cats.get(4, 0)}\n"
        f"🚫 Категория -1 (Невыполнимые): {cats.get(-1, 0)}\n"
        f"⏳ Не проанализированы: {stats['unanalyzed']}\n\n"
        f"💰 Средний бюджет: {stats['avg_budget']:,} ₽\n\n"
        f"🔥 <b>Топ тегов:</b>\n{tags_text}\n\n"
        f"📅 Обновлено: {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🏆 Категория 1", callback_data="cat_1"),
            InlineKeyboardButton(text="👍 Категория 2", callback_data="cat_2"),
        ],
        [
            InlineKeyboardButton(text="😐 Категория 3", callback_data="cat_3"),
            InlineKeyboardButton(text="👎 Категория 4", callback_data="cat_4"),
        ],
        [
            InlineKeyboardButton(text="🚫 Категория -1", callback_data="cat_-1"),
            InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh_stats"),
        ],
        [
            InlineKeyboardButton(text="◀️ Вернуться в главное меню", callback_data="back_to_menu"),
        ]
    ])

    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)

@dp.callback_query(lambda c: c.data == "refresh_stats")
async def refresh_stats(callback: CallbackQuery):
    stats = get_stats()
    cats = stats["by_category"]
    top_tags = get_top_tags(5)

    if top_tags:
        tags_text = "\n".join(f"   #{tag.replace(' ', '_')} — {count} заказов" for tag, count in top_tags)
    else:
        tags_text = "   Пока нет данных"

    text = (
        f"📊 <b>Сводка по заказам</b>\n\n"
        f"📦 Всего в базе: {stats['total']}\n\n"
        f"🏆 Категория 1 (Отличные): {cats.get(1, 0)}\n"
        f"👍 Категория 2 (Неплохие): {cats.get(2, 0)}\n"
        f"😐 Категория 3 (Средние): {cats.get(3, 0)}\n"
        f"👎 Категория 4 (Слабые): {cats.get(4, 0)}\n"
        f"🚫 Категория -1 (Невыполнимые): {cats.get(-1, 0)}\n"
        f"⏳ Не проанализированы: {stats['unanalyzed']}\n\n"
        f"💰 Средний бюджет: {stats['avg_budget']:,} ₽\n\n"
        f"🔥 <b>Топ тегов:</b>\n{tags_text}\n\n"
        f"📅 Обновлено: {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    )

    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=callback.message.reply_markup)
    except Exception:
        pass

    await callback.answer("✅ Обновлено!")

@dp.message(Command("menu"))
async def cmd_menu(message: Message):
    text = (
        "👋 <b>Привет! Я твой ассистент по фрилансу.</b>\n\n"
        "Что умею:\n"
        "📊 <b>Сводка</b> — статистика по всем заказам в базе\n"
        "📂 <b>Заказы по категориям</b> — просмотр и перелистывание заказов\n"
        "🔍 <b>Обработать заказ</b> — анализ заказа по ссылке с Kwork\n"
        "✍️ <b>Сгенерировать отклик</b> — готовый отклик по ТЗ заказчика\n"
        "🔎 <b>Поиск заказов</b> — поиск по ключевым словам и фильтрам\n\n"
        "Выбери нужный пункт 👇"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=get_main_menu())

@dp.callback_query(lambda c: c.data == "stats")
async def cb_stats(callback: CallbackQuery):
    await cmd_stats(callback.message)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "view_categories")
async def cb_view_categories(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🏆 Категория 1", callback_data="cat_1"),
            InlineKeyboardButton(text="👍 Категория 2", callback_data="cat_2"),
        ],
        [
            InlineKeyboardButton(text="😐 Категория 3", callback_data="cat_3"),
            InlineKeyboardButton(text="👎 Категория 4", callback_data="cat_4"),
        ],
        [
            InlineKeyboardButton(text="🚫 Категория -1", callback_data="cat_-1"),
        ],
        [
            InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu"),
        ]
    ])
    await callback.message.edit_text(
        "📂 <b>Выбери категорию для просмотра:</b>",
        parse_mode="HTML",
        reply_markup=keyboard
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "process_order")
async def cb_process_order(callback: CallbackQuery):
    await callback.message.edit_text(
        "🔍 <b>Обработка заказа</b>\n\n"
        "Отправь ссылку на заказ с Kwork и я его проанализирую.\n"
        "Например: <code>https://kwork.ru/projects/1234567</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu")]
        ])
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "generate_response")
async def cb_generate_response(callback: CallbackQuery):
    await callback.message.edit_text(
        "✍️ <b>Генерация отклика</b>\n\n"
        "Кнопка <b>«✍️ Сгенерировать отклик ✅»</b> появляется под каждым заказом.\n\n"
        "Просто открой нужный заказ через <b>«📂 Заказы по категориям»</b> "
        "или дождись нового уведомления — и нажми кнопку под ним.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📂 Перейти к заказам", callback_data="view_categories")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu")]
        ])
    )
    await callback.answer()

# @dp.callback_query(lambda c: c.data == "search_orders")
# async def cb_search_orders(callback: CallbackQuery):
#     await callback.message.edit_text(
#         "🔎 <b>Поиск заказов</b>\n\n"
#         "Отправь ключевое слово для поиска по заказам в базе.\n"
#         "Например: <code>python</code> или <code>wordpress</code>",
#         parse_mode="HTML",
#         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
#             [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu")]
#         ])
#     )
#     await callback.answer()

@dp.callback_query(lambda c: c.data == "back_to_menu")
async def cb_back_to_menu(callback: CallbackQuery):
    await callback.message.edit_text(
        "👋 <b>Главное меню</b>\n\nВыбери нужный пункт 👇",
        parse_mode="HTML",
        reply_markup=get_main_menu()
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "search_orders")
async def cb_search_orders(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔤 По ключевому слову", callback_data="search_keyword")],
        [InlineKeyboardButton(text="🎛 По фильтрам", callback_data="search_filters")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu")],
    ])
    await callback.message.edit_text(
        "🔎 <b>Поиск заказов</b>\n\nВыбери способ поиска:",
        parse_mode="HTML",
        reply_markup=keyboard
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "search_keyword")
async def cb_search_keyword(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SearchState.waiting_keyword)
    await callback.message.edit_text(
        "🔤 <b>Поиск по ключевому слову</b>\n\n"
        "Введи слово или фразу — найду все заказы где оно встречается в названии, описании или тегах.\n\n"
        "Например: <code>python</code>, <code>парсинг</code>, <code>wordpress</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="search_orders")]
        ])
    )
    await callback.answer()

@dp.message(SearchState.waiting_keyword)
async def handle_keyword_search(message: Message, state: FSMContext):
    keyword = message.text.strip()
    results = search_by_keyword(keyword)

    if not results:
        await message.answer(
            f"😔 По запросу <b>«{keyword}»</b> ничего не найдено.\n\nПопробуй другое слово.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔎 Новый поиск", callback_data="search_keyword")],
                [InlineKeyboardButton(text="🏠 Меню", callback_data="back_to_menu")],
            ])
        )
        await state.clear()
        return

    await state.set_state(SearchState.browsing_results)
    await state.update_data(results=results, index=0, source="search")

    text = format_project(results[0], 0, len(results))
    await message.answer(
        f"✅ Найдено заказов: <b>{len(results)}</b> по запросу «{keyword}»\n\n{text}",
        parse_mode="HTML",
        reply_markup=get_search_nav_keyboard(0, len(results))
    )


# ───────── Поиск по фильтрам ─────────

@dp.callback_query(lambda c: c.data == "search_filters")
async def cb_search_filters(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SearchState.filter_category)
    await state.update_data(filter_category=None, filter_tag=None,
                            filter_min=None, filter_max=None)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🏆 1", callback_data="fc_1"),
            InlineKeyboardButton(text="👍 2", callback_data="fc_2"),
            InlineKeyboardButton(text="😐 3", callback_data="fc_3"),
        ],
        [
            InlineKeyboardButton(text="👎 4", callback_data="fc_4"),
            InlineKeyboardButton(text="🚫 -1", callback_data="fc_-1"),
        ],
        [InlineKeyboardButton(text="✅ Неважно", callback_data="fc_any")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="search_orders")],
    ])
    await callback.message.edit_text(
        "🎛 <b>Фильтр — шаг 1/3</b>\n\nВыбери категорию заказов:",
        parse_mode="HTML",
        reply_markup=keyboard
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("fc_"))
async def cb_filter_category(callback: CallbackQuery, state: FSMContext):
    val = callback.data.split("fc_")[1]
    category = None if val == "any" else int(val)
    await state.update_data(filter_category=category)
    await state.set_state(SearchState.filter_tag)

    top_tags = get_top_tags(8)
    tag_buttons = []
    row = []
    for i, (tag, count) in enumerate(top_tags):
        row.append(InlineKeyboardButton(text=f"{tag} ({count})", callback_data=f"ft_{tag}"))
        if len(row) == 2:
            tag_buttons.append(row)
            row = []
    if row:
        tag_buttons.append(row)

    tag_buttons.append([InlineKeyboardButton(text="✅ Неважно", callback_data="ft_any")])
    tag_buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="search_filters")])

    await callback.message.edit_text(
        "🎛 <b>Фильтр — шаг 2/3</b>\n\nВыбери тег (или нажми Неважно):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=tag_buttons)
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("ft_"))
async def cb_filter_tag(callback: CallbackQuery, state: FSMContext):
    val = callback.data.split("ft_")[1]
    tag = None if val == "any" else val
    await state.update_data(filter_tag=tag)
    await state.set_state(SearchState.filter_budget)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="до 3 000 ₽", callback_data="fb_0_3000"),
            InlineKeyboardButton(text="3-10 000 ₽", callback_data="fb_3000_10000"),
        ],
        [
            InlineKeyboardButton(text="10-30 000 ₽", callback_data="fb_10000_30000"),
            InlineKeyboardButton(text="30-100 000 ₽", callback_data="fb_30000_100000"),
        ],
        [
            InlineKeyboardButton(text="от 100 000 ₽", callback_data="fb_100000_0"),
            InlineKeyboardButton(text="✅ Неважно", callback_data="fb_any"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="search_filters")],
    ])
    await callback.message.edit_text(
        "🎛 <b>Фильтр — шаг 3/3</b>\n\nВыбери диапазон бюджета заказчика:",
        parse_mode="HTML",
        reply_markup=keyboard
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("fb_"))
async def cb_filter_budget(callback: CallbackQuery, state: FSMContext):
    val = callback.data.split("fb_")[1]

    if val == "any":
        min_b, max_b = None, None
    else:
        parts = val.split("_")
        min_b = int(parts[0]) if parts[0] != "0" else None
        max_b = int(parts[1]) if parts[1] != "0" else None

    await state.update_data(filter_min=min_b, filter_max=max_b)
    data = await state.get_data()

    results = search_by_filters(
        category=data.get("filter_category"),
        min_budget=data.get("filter_min"),
        max_budget=data.get("filter_max"),
        tag=data.get("filter_tag")
    )

    if not results:
        await callback.message.edit_text(
            "😔 <b>По заданным фильтрам ничего не найдено</b>\n\nПопробуй изменить фильтры.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🎛 Новый поиск", callback_data="search_filters")],
                [InlineKeyboardButton(text="🏠 Меню", callback_data="back_to_menu")],
            ])
        )
        await state.clear()
        await callback.answer()
        return

    await state.set_state(SearchState.browsing_results)
    await state.update_data(results=results, index=0)

    text = format_project(results[0], 0, len(results))
    await callback.message.edit_text(
        f"✅ Найдено заказов: <b>{len(results)}</b>\n\n{text}",
        parse_mode="HTML",
        reply_markup=get_search_nav_keyboard(0, len(results))
    )
    await callback.answer()


# ───────── Навигация в результатах поиска ─────────

def get_search_nav_keyboard(index: int, total: int) -> InlineKeyboardMarkup:
    nav_row = []
    if index > 0:
        nav_row.append(InlineKeyboardButton(text="◀️ Пред.", callback_data="sr_prev"))
    if index < total - 1:
        nav_row.append(InlineKeyboardButton(text="След. ▶️", callback_data="sr_next"))

    buttons = []
    if nav_row:
        buttons.append(nav_row)
    buttons.append([
        InlineKeyboardButton(text="🔎 Новый поиск", callback_data="search_orders"),
        InlineKeyboardButton(text="🏠 Меню", callback_data="back_to_menu"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@dp.callback_query(lambda c: c.data in ["sr_prev", "sr_next"])
async def cb_search_navigate(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    results = data["results"]
    index = data["index"]

    index = index + 1 if callback.data == "sr_next" else index - 1
    index = max(0, min(index, len(results) - 1))
    await state.update_data(index=index)

    text = format_project(results[index], index, len(results))
    try:
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=get_search_nav_keyboard(index, len(results))
        )
    except Exception:
        pass
    await callback.answer()


# ─────────────────────────────────────────────
# Запуск
# ─────────────────────────────────────────────

async def main():
    bot = Bot(token=TOKEN)
    await bot.send_message(chat_id=MY_CHAT_ID, text="✅ Бот работает!")

    text = (
        "👋 <b>Привет! Я твой ассистент по фрилансу.</b>\n\n"
        "Что умею:\n"
        "📊 <b>Сводка</b> — статистика по всем заказам в базе\n"
        "📂 <b>Заказы по категориям</b> — просмотр и перелистывание заказов\n"
        "🔍 <b>Обработать заказ</b> — анализ заказа по ссылке с Kwork\n"
        "✍️ <b>Сгенерировать отклик</b> — готовый отклик по ТЗ заказчика\n"
        "🔎 <b>Поиск заказов</b> — поиск по ключевым словам и фильтрам\n\n"
        "Выбери нужный пункт 👇"
    )
    await bot.send_message(chat_id=MY_CHAT_ID, text=text, parse_mode="HTML", reply_markup=get_main_menu())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
