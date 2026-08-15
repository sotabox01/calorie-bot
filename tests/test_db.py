import json
import pytest

from db import Database

USER_ID = 42


@pytest.fixture
def db():
    database = Database()
    yield database
    database.close()


def _add_entry(database: Database, kcal=500.0, protein=30.0, fat=10.0, carbs=50.0,
               raw="test food"):
    items = [{"name": "food", "weight_g": 100.0, "weight_type": "exact",
              "kcal": kcal, "protein_g": protein, "fat_g": fat, "carbs_g": carbs}]
    database.add_entry(USER_ID, items, kcal, protein, fat, carbs, raw)
    return items


class TestAddAndGet:
    def test_add_entry_appears_in_today(self, db):
        _add_entry(db)
        entries = db.get_today_entries(USER_ID)
        assert len(entries) == 1
        assert entries[0]["raw_text"] == "test food"

    def test_get_today_totals(self, db):
        _add_entry(db, kcal=300, protein=20)
        _add_entry(db, kcal=200, protein=15)
        totals = db.get_today_totals(USER_ID)
        assert totals["kcal"] == pytest.approx(500)
        assert totals["protein"] == pytest.approx(35)

    def test_today_totals_empty(self, db):
        totals = db.get_today_totals(USER_ID)
        assert totals["kcal"] == 0

    def test_items_json_round_trip(self, db):
        items = [{"name": "egg", "weight_g": 60.0, "weight_type": "exact",
                  "kcal": 85.0, "protein_g": 7.0, "fat_g": 5.9, "carbs_g": 0.6}]
        db.add_entry(USER_ID, items, 85, 7, 5.9, 0.6, "egg")
        entry = db.get_today_entries(USER_ID)[0]
        restored = json.loads(entry["items_json"])
        assert restored[0]["name"] == "egg"
        assert restored[0]["kcal"] == 85.0


class TestDeleteTodayEntries:
    def test_delete_removes_all_today(self, db):
        _add_entry(db)
        _add_entry(db)
        db.delete_today_entries(USER_ID)
        assert db.get_today_entries(USER_ID) == []

    def test_delete_noop_when_empty(self, db):
        db.delete_today_entries(USER_ID)
        assert db.get_today_entries(USER_ID) == []


class TestUndoLastEntries:
    def test_undo_one_entry(self, db):
        _add_entry(db, raw="entry1")
        _add_entry(db, raw="entry2")
        deleted = db.undo_last_entries(USER_ID, 1)
        assert deleted is not None
        assert len(deleted) == 1
        assert deleted[0] == "entry2"
        assert len(db.get_today_entries(USER_ID)) == 1

    def test_undo_multiple_entries(self, db):
        _add_entry(db, raw="entry1")
        _add_entry(db, raw="entry2")
        _add_entry(db, raw="entry3")
        deleted = db.undo_last_entries(USER_ID, 2)
        assert deleted is not None
        assert len(deleted) == 2
        assert len(db.get_today_entries(USER_ID)) == 1

    def test_undo_returns_none_when_not_enough(self, db):
        _add_entry(db, raw="only_entry")
        result = db.undo_last_entries(USER_ID, 3)
        assert result is None
        assert len(db.get_today_entries(USER_ID)) == 1  # unchanged

    def test_undo_returns_none_when_empty(self, db):
        result = db.undo_last_entries(USER_ID, 1)
        assert result is None

    def test_undo_exact_count(self, db):
        _add_entry(db, raw="e1")
        _add_entry(db, raw="e2")
        deleted = db.undo_last_entries(USER_ID, 2)
        assert deleted is not None
        assert len(deleted) == 2
        assert db.get_today_entries(USER_ID) == []

    def test_undo_returns_raw_texts_most_recent_first(self, db):
        _add_entry(db, raw="first")
        _add_entry(db, raw="second")
        deleted = db.undo_last_entries(USER_ID, 2)
        assert deleted[0] == "second"
        assert deleted[1] == "first"


class TestGetLastEntryItems:
    def test_returns_items_from_last_entry(self, db):
        _add_entry(db, raw="first")
        items2 = [{"name": "chicken", "weight_g": 200.0, "weight_type": "exact",
                   "kcal": 330.0, "protein_g": 62.0, "fat_g": 7.0, "carbs_g": 0.0}]
        db.add_entry(USER_ID, items2, 330, 62, 7, 0, "second")
        result = db.get_last_entry_items(USER_ID)
        assert result is not None
        assert result[0]["name"] == "chicken"

    def test_returns_none_when_no_entries(self, db):
        assert db.get_last_entry_items(USER_ID) is None


class TestImportFoods:
    def test_import_basic(self, db):
        items = [{"name": "гречка варёная", "weight_g": 100.0,
                  "kcal": 110, "protein_g": 4, "fat_g": 1, "carbs_g": 22}]
        db.import_foods(USER_ID, items)
        refs = db.get_food_references(USER_ID)
        assert len(refs) == 1
        assert refs[0]["name"] == "гречка варёная"
        assert refs[0]["kcal_per_100"] == pytest.approx(110)

    def test_import_normalizes_to_per_100g(self, db):
        items = [{"name": "рис", "weight_g": 200.0,
                  "kcal": 220, "protein_g": 8, "fat_g": 2, "carbs_g": 44}]
        db.import_foods(USER_ID, items)
        refs = db.get_food_references(USER_ID)
        assert refs[0]["kcal_per_100"] == pytest.approx(110)
        assert refs[0]["protein_per_100"] == pytest.approx(4)

    def test_import_upserts(self, db):
        v1 = [{"name": "курица", "weight_g": 100.0,
               "kcal": 150, "protein_g": 20, "fat_g": 8, "carbs_g": 0}]
        v2 = [{"name": "курица", "weight_g": 100.0,
               "kcal": 165, "protein_g": 22, "fat_g": 7, "carbs_g": 0}]
        db.import_foods(USER_ID, v1)
        db.import_foods(USER_ID, v2)
        refs = db.get_food_references(USER_ID)
        assert len(refs) == 1
        assert refs[0]["kcal_per_100"] == pytest.approx(165)

    def test_import_skips_empty_name(self, db):
        items = [{"name": "", "weight_g": 100.0,
                  "kcal": 100, "protein_g": 5, "fat_g": 2, "carbs_g": 10}]
        db.import_foods(USER_ID, items)
        assert db.get_food_references(USER_ID) == []


class TestRecalcIngredientsFromRefs:
    def _setup_ref(self, db, name="курица", kcal=165, protein=22, fat=7, carbs=0):
        db.import_foods(USER_ID, [{"name": name, "weight_g": 100.0,
                                   "kcal": kcal, "protein_g": protein,
                                   "fat_g": fat, "carbs_g": carbs}])

    def test_recalc_reference_ingredient(self, db):
        self._setup_ref(db, name="курица", kcal=165, protein=22, fat=7, carbs=0)
        ingredients = [{"name": "курица", "weight_g": 200, "source": "reference",
                        "kcal": 0, "protein_g": 0, "fat_g": 0, "carbs_g": 0}]
        result = db.recalc_ingredients_from_refs(USER_ID, ingredients)
        assert result[0]["kcal"] == pytest.approx(330)
        assert result[0]["protein_g"] == pytest.approx(44)
        assert result[0]["fat_g"] == pytest.approx(14)

    def test_non_reference_ingredient_unchanged(self, db):
        self._setup_ref(db)
        ingredients = [{"name": "курица", "weight_g": 200, "source": "estimated",
                        "kcal": 999, "protein_g": 99, "fat_g": 9, "carbs_g": 0}]
        result = db.recalc_ingredients_from_refs(USER_ID, ingredients)
        assert result[0]["kcal"] == 999

    def test_missing_reference_ingredient_unchanged(self, db):
        ingredients = [{"name": "неизвестный", "weight_g": 100, "source": "reference",
                        "kcal": 500, "protein_g": 50, "fat_g": 5, "carbs_g": 0}]
        result = db.recalc_ingredients_from_refs(USER_ID, ingredients)
        assert result[0]["kcal"] == 500

    def test_case_insensitive_lookup(self, db):
        self._setup_ref(db, name="Курица Грудка", kcal=100, protein=20, fat=2, carbs=0)
        ingredients = [{"name": "курица грудка", "weight_g": 100, "source": "reference",
                        "kcal": 0, "protein_g": 0, "fat_g": 0, "carbs_g": 0}]
        result = db.recalc_ingredients_from_refs(USER_ID, ingredients)
        assert result[0]["kcal"] == pytest.approx(100)


class TestUserSettings:
    def test_set_and_get_goal(self, db):
        db.set_user_goal(USER_ID, 2000, 150)
        us = db.get_user_settings(USER_ID)
        assert us is not None
        assert us["daily_kcal"] == 2000
        assert us["daily_protein"] == 150

    def test_get_settings_none_before_set(self, db):
        assert db.get_user_settings(USER_ID) is None

    def test_hide_nutrients_default_false(self, db):
        assert db.get_hide_nutrients(USER_ID) is False

    def test_set_hide_nutrients_toggle(self, db):
        db.set_hide_nutrients(USER_ID, True)
        assert db.get_hide_nutrients(USER_ID) is True
        db.set_hide_nutrients(USER_ID, False)
        assert db.get_hide_nutrients(USER_ID) is False

    def test_notify_goals_default_false(self, db):
        assert db.get_notify_goals(USER_ID) is False

    def test_set_notify_goals_toggle(self, db):
        db.set_notify_goals(USER_ID, True)
        assert db.get_notify_goals(USER_ID) is True
        db.set_notify_goals(USER_ID, False)
        assert db.get_notify_goals(USER_ID) is False


class TestPendingRecipe:
    def test_set_and_get(self, db):
        data = {"dish_name": "Суп", "kcal_100": 50, "protein_100": 5,
                "fat_100": 2, "carbs_100": 6, "ingredients": []}
        db.set_pending_recipe(USER_ID, data)
        result = db.get_pending_recipe(USER_ID)
        assert result is not None
        assert result["dish_name"] == "Суп"
        assert result["kcal_100"] == 50

    def test_clear(self, db):
        db.set_pending_recipe(USER_ID, {"dish_name": "x", "kcal_100": 1,
                                         "protein_100": 1, "fat_100": 1,
                                         "carbs_100": 1, "ingredients": []})
        db.set_pending_recipe(USER_ID, None)
        assert db.get_pending_recipe(USER_ID) is None

    def test_none_by_default(self, db):
        assert db.get_pending_recipe(USER_ID) is None


class TestFoodReferenceText:
    def test_empty_when_no_refs(self, db):
        assert db.get_food_reference_text(USER_ID) == ""

    def test_contains_food_name_and_kcal(self, db):
        db.import_foods(USER_ID, [{"name": "яйцо", "weight_g": 100.0,
                                    "kcal": 155, "protein_g": 13,
                                    "fat_g": 11, "carbs_g": 1}])
        text = db.get_food_reference_text(USER_ID)
        assert "яйцо" in text
        assert "155" in text
