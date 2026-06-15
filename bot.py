"""
CalorieBot — Telegram бот для учёта калорий через OpenRouter LLM.

Поток:
1. Пользователь пишет, что съел → LLM парсит в структурированные данные
2. Сохраняем в SQLite (за день)
3. Отвечаем: что записано + итог дня + остаток до цели
"""

import json
import logging
from datetime import date

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

from config import settings
from db import Database
from llm_parser import LLMParser, IMPORT_SYSTEM_PROMPT
from recipe_prompt import RECIPE_SYSTEM_PROMPT

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
)
logger = logging.getLogger(__name__)

db = Database()
parser = LLMParser()


# ── Хендлеры ────────────────────────────────────────────────


async def start(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Приветствие."""
    await update.message.reply_text(
        "🥗 Привет! Я CalorieBot.\n\n"
        "Просто пиши, что ты съел, а я посчитаю калории и БЖУ.\n\n"
        "Примеры:\n"
        "• «200г гречки, 150г куриной грудки, салат с маслом»\n"
        "• «миска 350г, вес 680г — это суп с курицей»\n"
        "• «яйцо 2шт, хлеб 30г, масло 10г»\n\n"
        "Команды:\n"
        "• /goal 1800 120 — установить цели (ккал, белок)\n"
        "• /recipe — разобрать рецепт и рассчитать КБЖУ блюда\n"
        "• /import — импортировать свои КБЖУ продуктов\n"
        "• /save — сохранить продукты из последней записи в референсы\n"
        "• /today — итог за сегодня\n"
        "• /undo [N] — отменить N последних записей (без N = 1)\n"
        "• /reset — сбросить сегодняшние записи\n"
        "• /history — история дней с детализацией\n"
        "• /help — подробная справка по командам"
    )


async def help_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Подробная справка по командам."""
    await update.message.reply_text(
        "📖 CalorieBot — справка\n\n"
        "▸ /goal <ккал> <белок_г>\n"
        "  Установить дневные цели. Пример: /goal 1800 120\n"
        "  Без аргументов — показать текущие цели.\n\n"
        "▸ /recipe <описание рецепта>\n"
        "  Разобрать рецепт: ингредиенты с весом, итоговое КБЖУ, на 100г.\n"
        "  Пример: /recipe Курица 930г (16г б, 145 ккал). Готовая смесь 589г\n\n"
        "▸ /import <список продуктов>\n"
        "  Импортировать свои КБЖУ в базу референсов.\n"
        "  Пример: /import Гречка варёная — 100г: 110 ккал, 4г б, 2г ж, 23г у\n\n"
        "▸ /save [названия]\n"
        "  Сохранить продукты из последней записи в референсы.\n"
        "  Без аргументов — все продукты. С аргументами — фильтр по имени.\n\n"
        "▸ /today\n"
        "  Показать итог за сегодня без добавления записи.\n\n"
        "▸ /undo [N]\n"
        "  Отменить N последних записей. /undo = 1, /undo 2 = 2 последние.\n\n"
        "▸ /reset\n"
        "  Удалить все записи за сегодня.\n\n"
        "▸ /history\n"
        "  История дней с пагинацией и детализацией.\n\n"
        "💡 Как просто записать еду:\n"
        "  Пиши что и сколько съел — бот сам разберёт.\n"
        "  «куриная грудка 200г, гречка 150г, масло 10г»\n"
        "  «миска 350г, вес 680г — суп» (бот вычтет тару)\n"
        "  «яйцо 2шт» (оценит вес сам)\n\n"
        "📌 Иконки:\n"
        "  📌 = твои цифры, ✅ = из референсов, ⚡ = оценка ИИ"
    )


async def handle_message(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Главный обработчик: разобрать сообщение, сохранить, ответить итогом."""
    user_id = update.effective_user.id
    text = update.message.text.strip()

    if not text:
        return

    # ── 1. Парсинг через LLM с контекстом референсов ──
    ref_text = db.get_food_reference_text(user_id)

    try:
        items = await parser.parse(text, context=ref_text or None)
    except Exception as e:
        logger.exception("LLM parse error")
        await update.message.reply_text("❌ Ошибка при разборе текста. Попробуй ещё раз или напиши проще.")
        return

    if not items:
        await update.message.reply_text("🤷 Не вижу еды в сообщении. Напиши, что и сколько съел. Если хочешь добавить свои данные по продуктам — используй /import")
        return

    # ── 2. Суммируем ──
    total_kcal = sum(i["kcal"] for i in items)
    total_protein = sum(i["protein_g"] for i in items)
    total_fat = sum(i["fat_g"] for i in items)
    total_carbs = sum(i["carbs_g"] for i in items)

    # ── 3. Nag check (before save) ──
    from datetime import datetime, timedelta
    now = datetime.now()
    nag_msgs: list[str] = []
    last_ts = db.get_last_entry_time(user_id)
    if last_ts:
        last_dt = datetime.fromisoformat(last_ts)
        mins_since = (now - last_dt).total_seconds() / 60
        if mins_since < 30:
            nag_msgs.append(f"🤨 Ты же ел {int(mins_since)} мин назад. Может хватит?")
    two_h_ago = (now - timedelta(hours=2)).isoformat()
    snack_count = db.count_snacks_since(user_id, two_h_ago)
    if snack_count >= 3:
        nag_msgs.append(f"😤 {snack_count} перекуса за 2 часа! Хватит жевать.")
    elif snack_count == 2:
        nag_msgs.append(f"🤔 Уже {snack_count} перекуса за 2 часа...")

    # ── 4. Сохраняем ──
    db.add_entry(user_id, items, total_kcal, total_protein, total_fat, total_carbs, text)

    # ── 5. Итог дня ──
    today_totals = db.get_today_totals(user_id)
    us = db.get_user_settings(user_id)

    lines = ["✅ Записано:"]
    for i in items:
        wt = " (оценочно)" if i["weight_type"] == "estimated" else ""
        lines.append(f"• {i['name']} — {i["weight_g"]:.0f}г{wt} ({i["kcal"]:.0f} ккал, {i["protein_g"]:.1f}г б)")

    lines.append("")
    lines.append(f"📊 Итого за сегодня ({date.today().isoformat()}):")
    if us:
        lines.append(f"• Калории: {today_totals['kcal']:.0f} / {us['daily_kcal']:.0f} ккал")
        lines.append(f"• Белок: {today_totals['protein']:.1f} / {us['daily_protein']:.0f} г")
    else:
        lines.append(f"• Калории: {today_totals['kcal']:.0f} ккал")
        lines.append(f"• Белок: {today_totals['protein']:.1f} г")
    lines.append(f"• Жиры: {today_totals["fat"]:.1f} г")
    lines.append(f"• Углеводы: {today_totals["carbs"]:.1f} г")

    if us:
        rem_kcal = us["daily_kcal"] - today_totals["kcal"]
        rem_prot = us["daily_protein"] - today_totals["protein"]
        lines.append(f"📌 Осталось: {rem_kcal:.0f} ккал, {rem_prot:.1f}г белка")
    else:
        lines.append(f"💡 Установи цели: /goal 1800 120")

    for msg in nag_msgs:
        lines.append(msg)

    await update.message.reply_text("\n".join(lines))

async def today_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показать итог за сегодня без добавления записи."""
    user_id = update.effective_user.id
    totals = db.get_today_totals(user_id)
    us = db.get_user_settings(user_id)
    entries = db.get_today_entries(user_id)

    lines = [f"📊 Итого за сегодня ({date.today().isoformat()}):\n"]

    if not entries:
        lines.append("Пока ничего не записано.")
        await update.message.reply_text("\n".join(lines))
        return

    lines.append(f"Записей: {len(entries)}")
    lines.append(
        f"• Калории: {totals['kcal']:.0f}"
        + (f" / {us['daily_kcal']:.0f} ккал" if us else " ккал")
    )
    lines.append(
        f"• Белок: {totals['protein']:.1f}"
        + (f" / {us['daily_protein']:.0f} г" if us else " г")
    )
    lines.append(f"• Жиры: {totals['fat']:.1f} г")
    lines.append(f"• Углеводы: {totals['carbs']:.1f} г")

    if us:
        rem_kcal = us["daily_kcal"] - totals["kcal"]
        rem_prot = us["daily_protein"] - totals["protein"]
        lines.append(f"\n📌 Осталось: {rem_kcal:.0f} ккал, {rem_prot:.1f}г белка")

    await update.message.reply_text("\n".join(lines))


async def goal_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Установить дневные цели: /goal 1800 120"""
    user_id = update.effective_user.id
    args = context.args

    if len(args) < 2:
        us = db.get_user_settings(user_id)
        if us:
            await update.message.reply_text(
                f"Текущие цели: {us['daily_kcal']:.0f} ккал, {us['daily_protein']:.0f}г белка\n\n"
                f"Чтобы изменить: /goal <ккал> <белок_г>"
            )
        else:
            await update.message.reply_text(
                "Цели не установлены. Пример: /goal 1800 120"
            )
        return

    try:
        kcal = float(args[0].replace(",", "."))
        protein = float(args[1].replace(",", "."))
    except ValueError:
        await update.message.reply_text("Некорректные числа. Формат: /goal 1800 120")
        return

    db.set_user_goal(user_id, kcal, protein)
    await update.message.reply_text(f"✅ Цели установлены: {kcal:.0f} ккал, {protein:.0f}г белка")


async def reset_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сбросить все записи за сегодня."""
    user_id = update.effective_user.id
    today = date.today().isoformat()
    import sqlite3
    conn = sqlite3.connect(settings.db_path)
    conn.execute("DELETE FROM entries WHERE user_id = ? AND date = ?", (user_id, today))
    conn.commit()
    conn.close()
    await update.message.reply_text("🗑 Записи за сегодня удалены.")


async def undo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отменить N последних записей: /undo [N] (по умолч. 1)"""
    import sqlite3
    user_id = update.effective_user.id
    today = date.today().isoformat()

    # Сколько последних записей отменяем
    n = 1
    if context.args:
        try:
            n = int(context.args[0])
            if n < 1:
                await update.message.reply_text("🔢 Число должно быть ≥ 1. Пример: /undo 2")
                return
        except ValueError:
            await update.message.reply_text("🔢 Напиши число. Пример: /undo 2")
            return

    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, raw_text FROM entries WHERE user_id = ? AND date = ? ORDER BY timestamp DESC LIMIT ?",
        (user_id, today, n),
    ).fetchall()

    if n > len(rows):
        conn.close()
        await update.message.reply_text(
            f"🤷 У тебя всего {len(rows)} записей за сегодня. Нечего отменять."
        )
        return

    ids = [r["id"] for r in rows]
    placeholders = ",".join("?" * len(ids))
    conn.execute(f"DELETE FROM entries WHERE id IN ({placeholders})", ids)
    conn.commit()
    conn.close()

    if n == 1:
        await update.message.reply_text(f"🗑 Отменено: «{rows[0]['raw_text'][:60]}…»")
    else:
        await update.message.reply_text(f"🗑 Отменено {n} записей.")


async def save_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сохранить продукты из последней записи в референсы: /save [названия через пробел]

    Без аргументов — сохраняет все продукты из последней записи.
    С аргументами — сохраняет только те, что совпадают (по подстроке).
    """
    user_id = update.effective_user.id
    today = date.today().isoformat()
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT items_json FROM entries WHERE user_id = ? AND date = ? ORDER BY timestamp DESC LIMIT 1",
        (user_id, today),
    ).fetchone()

    if not row:
        conn.close()
        await update.message.reply_text("🤷 Нет записей за сегодня. Сначала напиши, что съел.")
        return

    items = json.loads(row["items_json"])
    filter_names = [a.lower() for a in context.args] if context.args else []

    to_save = []
    for i in items:
        name = i.get("name", "")
        if filter_names:
            if not any(f in name.lower() for f in filter_names):
                continue
        to_save.append(i)

    if not to_save:
        conn.close()
        await update.message.reply_text("🤷 Ничего не найдено для сохранения." if filter_names else "🤷 Нет продуктов в последней записи.")
        return

    added, updated = db.import_foods(user_id, to_save)
    conn.close()

    lines = [f"✅ Сохранено {len(to_save)} продуктов(а) в референсы:"]
    for i in to_save:
        kcal = i["kcal"]
        wt = i["weight_g"]
        prot = i["protein_g"]
        lines.append(f"  • {i['name']}: {kcal:.0f} ккал, {prot:.1f}г б (на {wt:.0f}г)")

    await update.message.reply_text("\n".join(lines))


async def recipe_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Разобрать рецепт и рассчитать КБЖУ готового блюда: /recipe <описание>

    Пример:
    /recipe Готовлю мясо мк2. Курица бедро 930г (16г б, 145 ккал). Гхи 5г. Лук 127г. Готовая смесь 589г
    """
    user_id = update.effective_user.id
    text = " ".join(context.args) if context.args else ""

    if not text:
        await update.message.reply_text(
            "📝 Разбор рецепта\n\n"
            "Напиши рецепт с ингредиентами и весом готового блюда.\n\n"
            "Пример:\n"
            "/recipe Готовлю мясо мк2. Курица бедро 930г (16г б, 145 ккал). "
            "Гхи 5г. Лук 127г. Готовая смесь 589г"
        )
        return

    ref_text = db.get_food_reference_text(user_id)

    try:
        result = await parser.parse(
            text,
            system=RECIPE_SYSTEM_PROMPT,
            context=ref_text or None,
            max_tokens=4096,
            timeout=60,
            raw=True,
        )
    except Exception as e:
        logger.exception("Recipe parse error")
        await update.message.reply_text(f"❌ Ошибка при разборе рецепта: {e}")
        return

    dish_name = result.get("dish_name", "Рецепт")
    ingredients = result.get("ingredients", [])
    totals = result.get("totals", {})
    per_100g = result.get("per_100g", {})
    new_refs = result.get("new_references", [])

    if not ingredients:
        await update.message.reply_text("🤷 Не смог разобрать рецепт.")
        return

    lines = [f"📋 {dish_name}"]
    lines.append("")

    src_icons = {"user_provided": "📌", "reference": "✅", "estimated": "⚡"}
    lines.append("Ингредиенты:")
    for i in ingredients:
        icon = src_icons.get(i.get("source", ""), "")
        lines.append(
            f"  {icon} {i['name']} — {i['weight_g']:.0f}г "
            f"({i['kcal']:.0f} ккал, {i['protein_g']:.1f}г б)"
        )

    lines.append("")
    lines.append(f"📊 Итого: {totals.get('kcal', 0):.0f} ккал, "
                 f"{totals.get('protein_g', 0):.1f}г б, "
                 f"{totals.get('fat_g', 0):.1f}г ж, "
                 f"{totals.get('carbs_g', 0):.1f}г у")

    if per_100g:
        cw = result.get("cooked_weight_g", 0)
        lines.append(f"   Вес готового блюда: {cw:.0f}г" if cw else "")
        lines.append(f"   На 100г: {per_100g.get('kcal', 0):.1f} ккал, "
                     f"{per_100g.get('protein_g', 0):.1f}г б")

    if new_refs:
        lines.append("")
        lines.append("📌 Новые продукты для сохранения:")
        for r in new_refs:
            lines.append(f"  • {r['name']}: {r['kcal']:.0f} ккал, "
                         f"{r['protein_g']:.1f}г б (на 100г)")

    lines.append("")
    lines.append("💡 Сохранить: /save <название>")

    await update.message.reply_text("\n".join(lines))


async def import_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Импорт своих продуктов и их КБЖУ: /import <текст с продуктами>

    Пример:
    /import Гречка варёная — 100г: 110 ккал, 4г б, 2г ж, 23г у
    Куриная грудка — 150г: 247 ккал, 46г б, 3г ж, 0г у
    """
    user_id = update.effective_user.id
    text = " ".join(context.args) if context.args else ""

    if not text:
        await update.message.reply_text(
            "📝 Импорт продуктов\n\n"
            "Пришли список своих продуктов с КБЖУ в одном сообщении вместе с /import.\n\n"
            "Формат — любой удобный, например:\n"
            "/import Гречка варёная — 100г: 110 ккал, 4г б, 2г ж, 23г у\n"
            "Куриная грудка — 150г: 247 ккал, 46г б, 3.6г ж, 0г у\n"
            "Оливковое масло — 15г: 134 ккал, 0г б, 15г ж, 0г у\n\n"
            "Можно просто скопировать свою выгрузку из чата."
        )
        return

    # Парсим через LLM со спец. промптом для импорта
    try:
        items = await parser.parse(text, system=IMPORT_SYSTEM_PROMPT, max_tokens=32000, timeout=120)
    except json.JSONDecodeError as e:
        logger.error("Import JSON error: %s", e)
        await update.message.reply_text(
            "❌ Ошибка: не смог разобрать ответ от ИИ. "
            "Попробуй сократить список или разбить на части."
        )
        return
    except httpx.TimeoutException:
        logger.error("Import timeout")
        await update.message.reply_text("❌ Таймаут при обращении к ИИ. Попробуй разбить список на части.")
        return
    except Exception as e:
        logger.exception("Import parse error: %s", e)
        await update.message.reply_text(
            f"❌ Ошибка при разборе списка продуктов: {e}. "
            "Попробуй другой формат."
        )
        return

    if not items:
        await update.message.reply_text("❌ Не смог распознать продукты. Попробуй в другом формате.")
        return

    # Сохраняем в референсы
    added, updated = db.import_foods(user_id, items)

    # Показываем, что сохранили
    lines = [f"✅ Импортировано продуктов: {len(items)}"]
    for i in items:
        name = i.get("name", "?")
        kcal = i.get("kcal", 0)
        prot = i.get("protein_g", 0)
        fat = i.get("fat_g", 0)
        carbs = i.get("carbs_g", 0)
        wt = i.get("weight_g", 100)
        lines.append(f"  • {name}: {kcal} ккал, {prot}г б, {fat}г ж, {carbs}г у (на {wt:.0f}г)")

    lines.append("")
    lines.append("📌 Теперь, когда ты пишешь что съел, бот будет использовать ТВОИ цифры.")

    await update.message.reply_text("\n".join(lines))


# ── History ─────────────────────────────────────────────────


WEEKDAYS_RU = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
DAYS_PER_PAGE = 5


def _build_history_keyboard(days: list[dict], page: int, total_pages: int) -> InlineKeyboardMarkup:
    """Build inline keyboard for history page."""
    kb = []
    for d in days:
        dt = date.fromisoformat(d["date"])
        wd = WEEKDAYS_RU[dt.weekday()]
        label = f"{wd}, {dt.day:02d}.{dt.month:02d} — {d['kcal']:.0f}/{d['protein']:.0f}г б"
        kb.append([InlineKeyboardButton(label.strip(), callback_data=f"history:day:{d['date']}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"history:page:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="history:nop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"history:page:{page + 1}"))
    if nav:
        kb.append(nav)

    return InlineKeyboardMarkup(kb)


async def history_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показать историю дней с пагинацией."""
    user_id = update.effective_user.id
    total_days = db.get_history_total_days(user_id)

    if total_days == 0:
        await update.message.reply_text("📭 История пуста. Начни записывать еду!")
        return

    total_pages = max(1, (total_days + DAYS_PER_PAGE - 1) // DAYS_PER_PAGE)
    days = db.get_history_days(user_id, limit=DAYS_PER_PAGE, offset=0)
    us = db.get_user_settings(user_id)

    lines = ["📅 История дней"]
    if us:
        lines.append(f"Цель: {us['daily_kcal']:.0f} ккал, {us['daily_protein']:.0f}г б")
    lines.append("")

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=_build_history_keyboard(days, 0, total_pages),
    )


async def history_callback(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle history inline button presses."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data = query.data

    if data == "history:nop":
        return

    parts = data.split(":")
    action = parts[1]

    if action == "page":
        page = int(parts[2])
        offset = page * DAYS_PER_PAGE
        total_days = db.get_history_total_days(user_id)
        total_pages = max(1, (total_days + DAYS_PER_PAGE - 1) // DAYS_PER_PAGE)
        days = db.get_history_days(user_id, limit=DAYS_PER_PAGE, offset=offset)
        us = db.get_user_settings(user_id)

        lines = ["📅 История дней"]
        if us:
            lines.append(f"Цель: {us['daily_kcal']:.0f} ккал, {us['daily_protein']:.0f}г б")
        lines.append("")

        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=_build_history_keyboard(days, page, total_pages),
        )

    elif action == "day":
        date_str = parts[2]
        entries = db.get_day_entries(user_id, date_str)
        us = db.get_user_settings(user_id)

        dt = date.fromisoformat(date_str)
        wd = WEEKDAYS_RU[dt.weekday()]
        header = f"📅 {wd}, {dt.day:02d}.{dt.month:02d}.{dt.year}"

        total_kcal = sum(e["total_kcal"] for e in entries)
        total_protein = sum(e["total_protein"] for e in entries)
        total_fat = sum(e["total_fat"] for e in entries)
        total_carbs = sum(e["total_carbs"] for e in entries)

        lines = [header, ""]
        lines.append(f"📊 Итого:")
        lines.append(f"  • Калории: {total_kcal:.0f}" + (f" / {us['daily_kcal']:.0f}" if us else ""))
        lines.append(f"  • Белок: {total_protein:.1f}" + (f" / {us['daily_protein']:.0f}" if us else ""))
        lines.append(f"  • Жиры: {total_fat:.1f} г")
        lines.append(f"  • Углеводы: {total_carbs:.1f} г")
        lines.append("")
        lines.append(f"📝 Записей: {len(entries)}")
        lines.append("")

        for i, e in enumerate(entries, 1):
            lines.append(f"{i}. «{e['raw_text']}»")
            lines.append(f"   {e['total_kcal']:.0f} ккал, {e['total_protein']:.1f}г б")

        # Add "Назад" button
        kb = [[InlineKeyboardButton("◀️ Назад к списку", callback_data="history:page:0")]]
        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(kb),
        )


USER_FILTER = filters.User(user_id=settings.owner_id)


def main() -> None:
    app = Application.builder().token(settings.bot_token).build()

    app.add_handler(CommandHandler("start", start, USER_FILTER))
    app.add_handler(CommandHandler("goal", goal_command, USER_FILTER))
    app.add_handler(CommandHandler("today", today_command, USER_FILTER))
    app.add_handler(CommandHandler("reset", reset_command, USER_FILTER))
    app.add_handler(CommandHandler("import", import_command, USER_FILTER))
    app.add_handler(CommandHandler("undo", undo_command, USER_FILTER))
    app.add_handler(CommandHandler("save", save_command, USER_FILTER))
    app.add_handler(CommandHandler("recipe", recipe_command, USER_FILTER))
    app.add_handler(CommandHandler("help", help_command, USER_FILTER))
    app.add_handler(CommandHandler("history", history_command, USER_FILTER))
    app.add_handler(CallbackQueryHandler(history_callback, pattern="^history:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & USER_FILTER, handle_message))

    logger.info("🚀 CalorieBot запущен")
    app.run_polling()


if __name__ == "__main__":
    main()