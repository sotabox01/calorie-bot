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

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

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
        "• /undo [N] — отменить N-ю запись (без N = последнюю)\n"
        "• /reset — сбросить сегодняшние записи\n"
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
        "  Отменить N-ю запись с конца. /undo = последнюю, /undo 2 = предпоследнюю.\n\n"
        "▸ /reset\n"
        "  Удалить все записи за сегодня.\n\n"
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
        await update.message.reply_text(
            "❌ Ошибка при разборе текста. Попробуй ещё раз или напиши проще."
        )
        return

    if not items:
        await update.message.reply_text(
            "🤷 Не вижу еды в сообщении. Напиши, что и сколько съел. "
            "Если хочешь добавить свои данные по продуктам — используй /import"
        )
        return

    # ── 2. Суммируем ──
    total_kcal = sum(i["kcal"] for i in items)
    total_protein = sum(i["protein_g"] for i in items)
    total_fat = sum(i["fat_g"] for i in items)
    total_carbs = sum(i["carbs_g"] for i in items)

    # ── 3. Сохраняем ──
    db.add_entry(user_id, items, total_kcal, total_protein, total_fat, total_carbs, text)

    # ── 4. Итог дня ──
    today_totals = db.get_today_totals(user_id)
    us = db.get_user_settings(user_id)

    lines = ["✅ Записано:"]
    for i in items:
        wt = " (оценочно)" if i["weight_type"] == "estimated" else ""
        lines.append(
            f"• {i['name']} — {i['weight_g']:.0f}г{wt} "
            f"({i['kcal']:.0f} ккал, {i['protein_g']:.1f}г б)"
        )

    lines.append("")
    lines.append(f"📊 Итого за сегодня ({date.today().isoformat()}):")
    lines.append(f"• Калории: {today_totals['kcal']:.0f} / {us['daily_kcal']:.0f} ккал"
                 if us else f"• Калории: {today_totals['kcal']:.0f} ккал")
    lines.append(f"• Белок: {today_totals['protein']:.1f} / {us['daily_protein']:.0f} г"
                 if us else f"• Белок: {today_totals['protein']:.1f} г")
    lines.append(f"• Жиры: {today_totals['fat']:.1f} г")
    lines.append(f"• Углеводы: {today_totals['carbs']:.1f} г")

    if us:
        rem_kcal = us["daily_kcal"] - today_totals["kcal"]
        rem_prot = us["daily_protein"] - today_totals["protein"]
        lines.append(f"")
        lines.append(f"📌 Осталось: {rem_kcal:.0f} ккал, {rem_prot:.1f}г белка")
    else:
        lines.append(f"")
        lines.append(f"💡 Установи цели: /goal 1800 120")

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
    """Отменить последнюю запись или N-ю с конца: /undo [N]"""
    user_id = update.effective_user.id
    today = date.today().isoformat()

    # Какую по счёту отменяем (1 = последняя)
    n = 1
    if context.args:
        try:
            n = int(context.args[0])
            if n < 1:
                await update.message.reply_text("🔢 Номер должен быть ≥ 1. Пример: /undo 2")
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
            f"🤷 У тебя всего {len(rows)} записей за сегодня. Используй /undo 1 … /undo {len(rows)}"
        )
        return

    row = rows[-1]  # n-я с конца = последняя в списке из LIMIT n
    conn.execute("DELETE FROM entries WHERE id = ?", (row["id"],))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"🗑 Отменено (#{n}): «{row['raw_text'][:60]}…»")


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


# ── Main ────────────────────────────────────────────────────


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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & USER_FILTER, handle_message))

    logger.info("🚀 CalorieBot запущен")
    app.run_polling()


if __name__ == "__main__":
    main()