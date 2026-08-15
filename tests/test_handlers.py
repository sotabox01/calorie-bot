import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def make_update(user_id=42):
    update = MagicMock()
    update.effective_user.id = user_id
    update.message.reply_text = AsyncMock()
    return update


def make_ctx(*args):
    ctx = MagicMock()
    ctx.args = list(args)
    return ctx


class TestResetCommand:
    async def test_calls_delete_today_entries(self):
        from bot import reset_command
        mock_db = MagicMock()
        update = make_update()

        with patch("bot.db", mock_db):
            await reset_command(update, make_ctx())

        mock_db.delete_today_entries.assert_called_once_with(42)
        update.message.reply_text.assert_awaited_once()

    async def test_no_direct_sqlite_used(self):
        """Verify reset_command no longer opens raw sqlite connections."""
        from bot import reset_command
        import sqlite3 as real_sqlite3

        mock_db = MagicMock()
        update = make_update()

        with patch("bot.db", mock_db), \
             patch("sqlite3.connect") as mock_connect:
            await reset_command(update, make_ctx())

        mock_connect.assert_not_called()


class TestUndoCommand:
    async def test_success_single(self):
        from bot import undo_command
        mock_db = MagicMock()
        mock_db.undo_last_entries.return_value = ["съел яблоко"]
        update = make_update()

        with patch("bot.db", mock_db):
            await undo_command(update, make_ctx())

        mock_db.undo_last_entries.assert_called_once_with(42, 1)
        msg = update.message.reply_text.call_args[0][0]
        assert "Отменено" in msg

    async def test_success_multiple(self):
        from bot import undo_command
        mock_db = MagicMock()
        mock_db.undo_last_entries.return_value = ["entry1", "entry2"]
        update = make_update()

        with patch("bot.db", mock_db):
            await undo_command(update, make_ctx("2"))

        mock_db.undo_last_entries.assert_called_once_with(42, 2)

    async def test_failure_shows_available_count(self):
        from bot import undo_command
        mock_db = MagicMock()
        mock_db.undo_last_entries.return_value = None
        mock_db.get_today_entries.return_value = [1, 2]  # 2 entries
        update = make_update()

        with patch("bot.db", mock_db):
            await undo_command(update, make_ctx("5"))

        mock_db.undo_last_entries.assert_called_once_with(42, 5)
        mock_db.get_today_entries.assert_called_once_with(42)
        msg = update.message.reply_text.call_args[0][0]
        assert "2" in msg

    async def test_rejects_n_zero(self):
        from bot import undo_command
        mock_db = MagicMock()
        update = make_update()

        with patch("bot.db", mock_db):
            await undo_command(update, make_ctx("0"))

        mock_db.undo_last_entries.assert_not_called()

    async def test_rejects_non_numeric(self):
        from bot import undo_command
        mock_db = MagicMock()
        update = make_update()

        with patch("bot.db", mock_db):
            await undo_command(update, make_ctx("abc"))

        mock_db.undo_last_entries.assert_not_called()


class TestSaveCommand:
    async def test_saves_pending_recipe_with_name(self):
        from bot import save_command
        mock_db = MagicMock()
        mock_db.get_pending_recipe.return_value = {
            "kcal_100": 120.0, "protein_100": 15.0, "fat_100": 5.0, "carbs_100": 8.0
        }
        mock_db.import_foods.return_value = (1, 0)
        update = make_update()

        with patch("bot.db", mock_db):
            await save_command(update, make_ctx("мясо", "мк2"))

        mock_db.import_foods.assert_called_once()
        saved_items = mock_db.import_foods.call_args[0][1]
        assert saved_items[0]["name"] == "мясо мк2"
        mock_db.set_pending_recipe.assert_called_once_with(42, None)

    async def test_pending_recipe_no_args_prompts_for_name(self):
        from bot import save_command
        mock_db = MagicMock()
        mock_db.get_pending_recipe.return_value = {
            "kcal_100": 120.0, "protein_100": 15.0, "fat_100": 5.0, "carbs_100": 8.0
        }
        update = make_update()

        with patch("bot.db", mock_db):
            await save_command(update, make_ctx())

        mock_db.import_foods.assert_not_called()

    async def test_fallback_to_last_entry(self):
        from bot import save_command
        mock_db = MagicMock()
        mock_db.get_pending_recipe.return_value = None
        mock_db.get_last_entry_items.return_value = [
            {"name": "яйцо", "weight_g": 60.0, "weight_type": "exact",
             "kcal": 85.0, "protein_g": 7.0, "fat_g": 5.9, "carbs_g": 0.6}
        ]
        mock_db.import_foods.return_value = (1, 0)
        update = make_update()

        with patch("bot.db", mock_db):
            await save_command(update, make_ctx())

        mock_db.get_last_entry_items.assert_called_once_with(42)
        mock_db.import_foods.assert_called_once()

    async def test_no_entries_today(self):
        from bot import save_command
        mock_db = MagicMock()
        mock_db.get_pending_recipe.return_value = None
        mock_db.get_last_entry_items.return_value = None
        update = make_update()

        with patch("bot.db", mock_db):
            await save_command(update, make_ctx())

        mock_db.import_foods.assert_not_called()
        update.message.reply_text.assert_awaited_once()


class TestGoalNotification:
    async def test_notification_sent_when_kcal_goal_crossed(self):
        from bot import handle_message
        mock_db = MagicMock()
        mock_parser = MagicMock()
        mock_parser.parse = AsyncMock(return_value=[{
            "name": "еда", "weight_g": 100, "weight_type": "exact",
            "kcal": 500, "protein_g": 30, "fat_g": 10, "carbs_g": 20,
        }])
        # prev totals: 1400 kcal (below goal 1800)
        # new totals: 1900 kcal (above goal 1800)
        mock_db.get_food_reference_text.return_value = ""
        mock_db.get_hide_nutrients.return_value = False
        mock_db.get_today_totals.side_effect = [
            {"kcal": 1400.0, "protein": 80.0, "fat": 40.0, "carbs": 100.0},  # prev
            {"kcal": 1900.0, "protein": 110.0, "fat": 50.0, "carbs": 120.0}, # after
        ]
        mock_db.get_user_settings.return_value = {
            "daily_kcal": 1800.0, "daily_protein": 120.0,
            "daily_fat": 60.0, "daily_carbs": 200.0,
        }
        mock_db.get_notify_goals.return_value = True

        update = make_update()

        with patch("bot.db", mock_db), patch("bot.parser", mock_parser):
            await handle_message(update, make_ctx())

        msg = update.message.reply_text.call_args[0][0]
        assert "Цель по калориям достигнута" in msg

    async def test_no_notification_when_notify_disabled(self):
        from bot import handle_message
        mock_db = MagicMock()
        mock_parser = MagicMock()
        mock_parser.parse = AsyncMock(return_value=[{
            "name": "еда", "weight_g": 100, "weight_type": "exact",
            "kcal": 500, "protein_g": 30, "fat_g": 10, "carbs_g": 20,
        }])
        mock_db.get_food_reference_text.return_value = ""
        mock_db.get_hide_nutrients.return_value = False
        mock_db.get_today_totals.side_effect = [
            {"kcal": 1400.0, "protein": 80.0, "fat": 40.0, "carbs": 100.0},
            {"kcal": 1900.0, "protein": 110.0, "fat": 50.0, "carbs": 120.0},
        ]
        mock_db.get_user_settings.return_value = {
            "daily_kcal": 1800.0, "daily_protein": 120.0,
            "daily_fat": 60.0, "daily_carbs": 200.0,
        }
        mock_db.get_notify_goals.return_value = False  # disabled

        update = make_update()

        with patch("bot.db", mock_db), patch("bot.parser", mock_parser):
            await handle_message(update, make_ctx())

        msg = update.message.reply_text.call_args[0][0]
        assert "Цель" not in msg
