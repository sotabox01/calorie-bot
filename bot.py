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

# ── Helpers ─────────────────────────────────────────────────



def calc_totals(ingredients: list[dict]) -> dict:
    return {
        "kcal": sum(float(i.get("kcal", 0) or 0) for i in ingredients),
        "protein_g": sum(float(i.get("protein_g", 0) or 0) for i in ingredients),
        "fat_g": sum(float(i.get("fat_g", 0) or 0) for i in ingredients),
        "carbs_g": sum(float(i.get("carbs_g", 0) or 0) for i in ingredients),
    }


# ── Хендлеры ────────────────────────────────────────────────


async def start(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🥗 Привет! Я CalorieBot.\n\n"
        "Просто пиши, что ты съел, а я посчитаю калории и БЖУ.\n\n"
        "Примеры:\n"
        "• «200г гречки, 150г куриной грудки, салат с маслом»\n"
        "• «миска 350г, вес 680г — это суп с курицей»\n"
        "• «яйцо 2шт, хлеб 30г, масло 10г»\n\n"
        "Команды:\n"
        "• /goal 1800 120 — установить цели (ккал, белок)\n"
        "• /recipe — разобрать рецепт, ничего не сохраняет\n"
        "• /import — импортировать свои КБЖУ продуктов\n"
        "• /save — сохранить рецепт или продукты в референсы\n"
        "• /today — итог за сегодня\n"
        "• /undo [N] — отменить N последних записей\n"
        "• /reset — сбросить сегодняшние записи\n"
        "• /history — история дней с детализацией\n"
        "• /settings — настройки\n"
        "• /help — подробная справка"
    )


async def help_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📖 CalorieBot — справка\n\n"
        "▸ /goal <ккал> <белок_г>\n"
        "  Установить дневные цели. /goal без аргументов — показать текущие.\n\n"
        "▸ /recipe <описание>\n"
        "  Разобрать рецепт. Ничего не сохраняется автоматически.\n"
        "  После /recipe используй /save <название> чтобы сохранить блюдо.\n\n"
        "▸ /import <список>\n"
        "  Импортировать свои КБЖУ продуктов.\n\n"
        "▸ /save [названия]\n"
        "  Если последняя команда была /recipe — сохраняет блюдо как референс.\n"
        "  Иначе — сохраняет продукты из последней записи еды.\n\n"
        "▸ /today — итог за сегодня\n"
        "▸ /undo [N] — отменить N последних записей\n"
        "▸ /reset — удалить записи за сегодня\n"
        "▸ /history — история дней\n"
        "▸ /settings — скрывать/показывать КБЖУ при записи\n\n"
        "💡 Просто пиши что съел: «куриная грудка 200г, гречка 150г»"
    )


async def handle_message(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = update.message.text.strip()

    if not text:
        return

    ref_text = db.get_food_reference_text(user_id)

    try:
        items = await parser.parse(text, context=ref_text or None)
    except Exception:
        logger.exception("LLM parse error")
        await update.message.reply_text("❌ Ошибка при разборе текста. Попробуй ещё раз или напиши проще.")
        return

    if not items:
        await update.message.reply_text("🤷 Не вижу еды в сообщении. Напиши, что и сколько съел. Если хочешь добавить свои данные по продуктам — используй /import")
        return

    total_kcal = sum(i['kcal'] for i in items)
    total_protein = sum(i['protein_g'] for i in items)
    total_fat = sum(i['fat_g'] for i in items)
    total_carbs = sum(i['carbs_g'] for i in items)

    db.add_entry(user_id, items, total_kcal, total_protein, total_fat, total_carbs, text)

    hide = db.get_hide_nutrients(user_id)

    if hide:
        food_names = [i['name'] for i in items]
        await update.message.reply_text(f"✅ Записано: {', '.join(food_names)}")
        return

    today_totals = db.get_today_totals(user_id)
    us = db.get_user_settings(user_id)

    lines = ["✅ Записано:"]
    for i in items:
        wt = " (оценочно)" if i['weight_type'] == "estimated" else ""
        lines.append(f"• {i['name']} — {i['weight_g']:.0f}г{wt} ({i['kcal']:.0f} ккал, {i['protein_g']:.1f}г б)")

    lines.append("")
    lines.append(f"📊 Итого за сегодня ({date.today().isoformat()}):")
    if us:
        lines.append(f"• Калории: {today_totals['kcal']:.0f} / {us['daily_kcal']:.0f} ккал")
        lines.append(f"• Белок: {today_totals['protein']:.1f} / {us['daily_protein']:.0f} г")
    else:
        lines.append(f"• Калории: {today_totals['kcal']:.0f} ккал")
        lines.append(f"• Белок: {today_totals['protein']:.1f} г")
    lines.append(f"• Жиры: {today_totals['fat']:.1f} г")
    lines.append(f"• Углеводы: {today_totals['carbs']:.1f} г")

    if us:
        rem_kcal = us['daily_kcal'] - today_totals['kcal']
        rem_prot = us['daily_protein'] - today_totals['protein']
        lines.append(f"📌 Осталось: {rem_kcal:.0f} ккал, {rem_prot:.1f}г белка")
    else:
        lines.append(f"💡 Установи цели: /goal 1800 120")

    await update.message.reply_text("\n".join(lines))


async def today_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
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
    lines.append(f"• Калории: {totals['kcal']:.0f}" + (f" / {us['daily_kcal']:.0f} ккал" if us else " ккал"))
    lines.append(f"• Белок: {totals['protein']:.1f}" + (f" / {us['daily_protein']:.0f} г" if us else " г"))
    lines.append(f"• Жиры: {totals['fat']:.1f} г")
    lines.append(f"• Углеводы: {totals['carbs']:.1f} г")

    if us:
        rem_kcal = us['daily_kcal'] - totals["kcal"]
        rem_prot = us['daily_protein'] - totals["protein"]
        lines.append(f"\n📌 Осталось: {rem_kcal:.0f} ккал, {rem_prot:.1f}г белка")

    await update.message.reply_text("\n".join(lines))


async def goal_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
            await update.message.reply_text("Цели не установлены. Пример: /goal 1800 120")
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
    user_id = update.effective_user.id
    db.delete_today_entries(user_id)
    await update.message.reply_text("🗑 Записи за сегодня удалены.")


async def undo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

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

    deleted = db.undo_last_entries(user_id, n)
    if deleted is None:
        today_count = len(db.get_today_entries(user_id))
        await update.message.reply_text(f"🤷 У тебя всего {today_count} записей за сегодня. Нечего отменять.")
        return

    if n == 1:
        await update.message.reply_text(f"🗑 Отменено: «{deleted[0][:60]}…»")
    else:
        await update.message.reply_text(f"🗑 Отменено {n} записей.")


async def save_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сохранить рецепт (pending_recipe) или продукты из последней записи.

    После /recipe: /save <название> — сохраняет блюдо как референс.
    Иначе — сохраняет продукты из последней записи еды.
    """
    user_id = update.effective_user.id

    # Check if there's a pending recipe first
    pending = db.get_pending_recipe(user_id)
    if pending and context.args:
        new_name = " ".join(context.args)
        db.import_foods(user_id, [{
            "name": new_name,
            "weight_g": 100,
            "kcal": pending["kcal_100"],
            "protein_g": pending["protein_100"],
            "fat_g": pending["fat_100"],
            "carbs_g": pending["carbs_100"],
        }])
        db.set_pending_recipe(user_id, None)
        await update.message.reply_text(
            f"✅ Блюдо «{new_name}» сохранено! "
            f"({pending['kcal_100']:.0f} ккал/100г, {pending['protein_100']:.1f}г б)"
        )
        return

    if pending and not context.args:
        await update.message.reply_text("📝 Напиши название после /save, например: /save грудка мк1")
        return

    # Fall back to saving from last entry
    items = db.get_last_entry_items(user_id)

    if not items:
        await update.message.reply_text("🤷 Нет записей за сегодня. Сначала напиши, что съел, или используй /recipe.")
        return

    filter_names = [a.lower() for a in context.args] if context.args else []

    to_save = []
    for i in items:
        name = i.get("name", "")
        if filter_names:
            if not any(f in name.lower() for f in filter_names):
                continue
        to_save.append(i)

    if not to_save:
        await update.message.reply_text("🤷 Ничего не найдено для сохранения." if filter_names else "🤷 Нет продуктов в последней записи.")
        return

    db.import_foods(user_id, to_save)

    lines = [f"✅ Сохранено {len(to_save)} продуктов(а) в референсы:"]
    for i in to_save:
        lines.append(f"  • {i['name']}: {i['kcal']:.0f} ккал, {i['protein_g']:.1f}г б (на {i['weight_g']:.0f}г)")

    await update.message.reply_text("\n".join(lines))


async def recipe_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Разобрать рецепт. Ничего не сохраняет.

    Потом /save <название> сохраняет блюдо в референсы.
    Все КБЖУ для reference-ингредиентов пересчитываются сервером из БД.
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
    per_100g = result.get("per_100g", {})

    if not ingredients:
        await update.message.reply_text("🤷 Не смог разобрать рецепт.")
        return

    # ── Server-side recalculation for reference ingredients ──
    ingredients = db.recalc_ingredients_from_refs(user_id, ingredients)
    totals = calc_totals(ingredients)

    cw = float(result.get("cooked_weight_g", 0) or 0)
    if cw > 0:
        pf = 100.0 / cw
        per_100g = {
            "kcal": round(totals["kcal"] * pf, 1),
            "protein_g": round(totals["protein_g"] * pf, 1),
            "fat_g": round(totals["fat_g"] * pf, 1),
            "carbs_g": round(totals["carbs_g"] * pf, 1),
        }

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
    lines.append(f"📊 Итого: {totals['kcal']:.0f} ккал, "
                 f"{totals['protein_g']:.1f}г б, "
                 f"{totals['fat_g']:.1f}г ж, "
                 f"{totals['carbs_g']:.1f}г у")

    if per_100g:
        lines.append(f"   Вес готового блюда: {cw:.0f}г" if cw else "")
        lines.append(f"   На 100г: {per_100g.get('kcal', 0):.1f} ккал, "
                     f"{per_100g.get('protein_g', 0):.1f}г б")

    # Save recipe data temporarily — only saved when user runs /save <name>
    db.set_pending_recipe(user_id, {
        "dish_name": dish_name,
        "kcal_100": per_100g.get("kcal", 0),
        "protein_100": per_100g.get("protein_g", 0),
        "fat_100": per_100g.get("fat_g", 0),
        "carbs_100": per_100g.get("carbs_g", 0),
        "ingredients": ingredients,
    } if per_100g else None)

    lines.append("")
    lines.append("💡 Сохранить: /save <название блюда>")

    await update.message.reply_text("\n".join(lines))


async def import_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = " ".join(context.args) if context.args else ""

    if not text:
        await update.message.reply_text(
            "📝 Импорт продуктов\n\n"
            "Пришли список своих продуктов с КБЖУ в одном сообщении вместе с /import.\n\n"
            "Формат — любой удобный, например:\n"
            "/import Гречка варёная — 100г: 110 ккал, 4г б, 2г ж, 23г у\n"
            "Куриная грудка — 150г: 247 ккал, 46г б, 3.6г ж, 0г у\n"
            "Можно просто скопировать свою выгрузку из чата."
        )
        return

    try:
        items = await parser.parse(text, system=IMPORT_SYSTEM_PROMPT, max_tokens=32000, timeout=120)
    except json.JSONDecodeError:
        logger.error("Import JSON error")
        await update.message.reply_text("❌ Ошибка: не смог разобрать ответ от ИИ. Попробуй сократить список.")
        return
    except Exception as e:
        logger.exception("Import parse error")
        await update.message.reply_text(f"❌ Ошибка при разборе: {e}. Попробуй другой формат.")
        return

    if not items:
        await update.message.reply_text("❌ Не смог распознать продукты. Попробуй в другом формате.")
        return

    db.import_foods(user_id, items)

    lines = [f"✅ Импортировано продуктов: {len(items)}"]
    for i in items:
        lines.append(f"  • {i.get('name', '?')}: {i.get('kcal', 0)} ккал, "
                     f"{i.get('protein_g', 0)}г б (на {i.get('weight_g', 100):.0f}г)")

    lines.append("")
    lines.append("📌 Теперь бот будет использовать ТВОИ цифры.")
    await update.message.reply_text("\n".join(lines))


# ── History ─────────────────────────────────────────────────


WEEKDAYS_RU = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
DAYS_PER_PAGE = 5


def _build_history_keyboard(days: list[dict], page: int, total_pages: int) -> InlineKeyboardMarkup:
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

    await update.message.reply_text("\n".join(lines), reply_markup=_build_history_keyboard(days, 0, total_pages))


async def history_callback(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
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

        await query.edit_message_text("\n".join(lines), reply_markup=_build_history_keyboard(days, page, total_pages))

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
        lines.append("📊 Итого:")
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

        kb = [[InlineKeyboardButton("◀️ Назад к списку", callback_data="history:page:0")]]
        await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))


# ── Settings ─────────────────────────────────────────────────


async def settings_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    hidden = db.get_hide_nutrients(user_id)

    status = "✅ Скрыты" if hidden else "❌ Показаны"
    lines = [
        "⚙️ Настройки",
        "",
        "При внесении записи о еде:",
        f"  {status} — калории и белок",
        "",
        "Если скрыты, в ответе будет только название продукта.",
        "Итоги дня — в /today.",
    ]

    btn_label = "🙈 Скрывать КБЖУ" if not hidden else "👀 Показывать КБЖУ"
    kb = [[InlineKeyboardButton(btn_label, callback_data="settings:toggle")]]
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))


async def settings_callback(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    if query.data == "settings:toggle":
        current = db.get_hide_nutrients(user_id)
        db.set_hide_nutrients(user_id, not current)

        hidden = not current
        status = "✅ Скрыты" if hidden else "❌ Показаны"
        lines = [
            "⚙️ Настройки",
            "",
            "При внесении записи о еде:",
            f"  {status} — калории и белок",
            "",
            "Если скрыты, в ответе будет только название продукта.",
            "Итоги дня — в /today.",
        ]
        btn_label = "🙈 Скрывать КБЖУ" if not hidden else "👀 Показывать КБЖУ"
        kb = [[InlineKeyboardButton(btn_label, callback_data="settings:toggle")]]
        await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))


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
    app.add_handler(CommandHandler("settings", settings_command, USER_FILTER))
    app.add_handler(CallbackQueryHandler(history_callback, pattern="^history:"))
    app.add_handler(CallbackQueryHandler(settings_callback, pattern="^settings:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & USER_FILTER, handle_message))

    logger.info("🚀 CalorieBot запущен")
    app.run_polling()


if __name__ == "__main__":
    main()