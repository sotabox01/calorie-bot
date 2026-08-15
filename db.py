import json
import sqlite3
import os
from datetime import date, datetime
from typing import Any

from config import settings


class Database:
    """SQLite database for food logging."""

    def __init__(self) -> None:
        os.makedirs(os.path.dirname(settings.db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(settings.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_db()

    def _init_db(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS entries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                date        TEXT    NOT NULL,
                timestamp   TEXT    NOT NULL,
                raw_text    TEXT    NOT NULL,
                items_json  TEXT    NOT NULL,
                total_kcal  REAL    NOT NULL,
                total_protein REAL  NOT NULL,
                total_fat   REAL    NOT NULL,
                total_carbs REAL    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_settings (
                user_id       INTEGER PRIMARY KEY,
                daily_kcal    REAL DEFAULT 1800,
                daily_protein REAL DEFAULT 120,
                daily_fat     REAL DEFAULT 60,
                daily_carbs   REAL DEFAULT 200
            );

            CREATE INDEX IF NOT EXISTS idx_entries_user_date
                ON entries(user_id, date);

            CREATE TABLE IF NOT EXISTS food_reference (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL,
                name            TEXT    NOT NULL,
                kcal_per_100    REAL NOT NULL,
                protein_per_100 REAL NOT NULL,
                fat_per_100     REAL NOT NULL,
                carbs_per_100   REAL NOT NULL,
                created_at      TEXT NOT NULL,
                UNIQUE(user_id, name)
            );

            CREATE INDEX IF NOT EXISTS idx_ref_user_name
                ON food_reference(user_id, name);
        """)
        self._conn.commit()

        # Migration: add columns that may not exist yet
        for table, col, col_type in [
            ("user_settings", "hide_nutrients", "INTEGER DEFAULT 0"),
            ("user_settings", "pending_recipe", "TEXT DEFAULT NULL"),
        ]:
            try:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
                self._conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists

    # ── Food entries ──────────────────────────────────────────

    def add_entry(
        self,
        user_id: int,
        items: list[dict[str, Any]],
        total_kcal: float,
        total_protein: float,
        total_fat: float,
        total_carbs: float,
        raw_text: str,
    ) -> None:
        today = date.today().isoformat()
        now = datetime.now().isoformat()
        self._conn.execute(
            """INSERT INTO entries
               (user_id, date, timestamp, raw_text, items_json,
                total_kcal, total_protein, total_fat, total_carbs)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                today,
                now,
                raw_text,
                json.dumps(items, ensure_ascii=False),
                total_kcal,
                total_protein,
                total_fat,
                total_carbs,
            ),
        )
        self._conn.commit()

    def get_today_entries(self, user_id: int) -> list[dict[str, Any]]:
        today = date.today().isoformat()
        rows = self._conn.execute(
            "SELECT * FROM entries WHERE user_id = ? AND date = ? ORDER BY timestamp, id",
            (user_id, today),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_today_totals(self, user_id: int) -> dict[str, float]:
        today = date.today().isoformat()
        row = self._conn.execute(
            """SELECT
                 COALESCE(SUM(total_kcal), 0)    AS kcal,
                 COALESCE(SUM(total_protein), 0) AS protein,
                 COALESCE(SUM(total_fat), 0)     AS fat,
                 COALESCE(SUM(total_carbs), 0)   AS carbs
               FROM entries WHERE user_id = ? AND date = ?""",
            (user_id, today),
        ).fetchone()
        return dict(row)

    # ── User settings ─────────────────────────────────────────

    def get_user_settings(self, user_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM user_settings WHERE user_id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None

    def set_user_goal(
        self, user_id: int, kcal: float, protein: float
    ) -> None:
        self._conn.execute(
            """INSERT INTO user_settings (user_id, daily_kcal, daily_protein)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                daily_kcal    = excluded.daily_kcal,
                daily_protein = excluded.daily_protein""",
            (user_id, kcal, protein),
        )
        self._conn.commit()

    def get_hide_nutrients(self, user_id: int) -> bool:
        row = self._conn.execute(
            "SELECT hide_nutrients FROM user_settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return bool(row["hide_nutrients"]) if row else False

    def set_hide_nutrients(self, user_id: int, value: bool) -> None:
        self._conn.execute(
            """INSERT INTO user_settings (user_id, hide_nutrients)
               VALUES (?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                hide_nutrients = excluded.hide_nutrients""",
            (user_id, int(value)),
        )
        self._conn.commit()

    def get_pending_recipe(self, user_id: int) -> dict | None:
        """Return recipe data from the last /recipe, or None."""
        row = self._conn.execute(
            "SELECT pending_recipe FROM user_settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row or not row["pending_recipe"]:
            return None
        return json.loads(row["pending_recipe"])

    def set_pending_recipe(self, user_id: int, data: dict | None) -> None:
        """Store recipe data temporarily, or clear it. /save <name> creates the reference."""
        self._conn.execute(
            """INSERT INTO user_settings (user_id, pending_recipe)
               VALUES (?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                pending_recipe = excluded.pending_recipe""",
            (
                user_id,
                json.dumps(data, ensure_ascii=False) if data is not None else None,
            ),
        )
        self._conn.commit()

    # ── Food reference ────────────────────────────────────────

    def import_foods(self, user_id: int, items: list[dict]) -> tuple[int, int]:
        added = 0
        updated = 0
        now = datetime.now().isoformat()
        for item in items:
            name = item.get("name", "").strip().lower()
            if not name:
                continue
            name = name.replace(" (оценочно)", "").replace(" (estimated)", "").strip()
            weight = float(item.get("weight_g", 100) or 100)
            kcal = float(item.get("kcal", 0))
            protein = float(item.get("protein_g", 0))
            fat = float(item.get("fat_g", 0))
            carbs = float(item.get("carbs_g", 0))
            factor = 100.0 / weight if weight > 0 else 1.0
            self._conn.execute(
                """INSERT INTO food_reference
                   (user_id, name, kcal_per_100, protein_per_100, fat_per_100, carbs_per_100, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, name) DO UPDATE SET
                     kcal_per_100    = excluded.kcal_per_100,
                     protein_per_100 = excluded.protein_per_100,
                     fat_per_100     = excluded.fat_per_100,
                     carbs_per_100   = excluded.carbs_per_100,
                     created_at      = excluded.created_at""",
                (user_id, name, round(kcal * factor, 1),
                 round(protein * factor, 1), round(fat * factor, 1),
                 round(carbs * factor, 1), now),
            )
            added += 1
        self._conn.commit()
        return added, updated

    def delete_today_entries(self, user_id: int) -> None:
        today = date.today().isoformat()
        self._conn.execute(
            "DELETE FROM entries WHERE user_id = ? AND date = ?", (user_id, today)
        )
        self._conn.commit()

    def undo_last_entries(self, user_id: int, n: int) -> list[str] | None:
        """Delete n most recent entries for today. Returns raw_texts, or None if fewer than n exist."""
        today = date.today().isoformat()
        rows = self._conn.execute(
            "SELECT id, raw_text FROM entries WHERE user_id = ? AND date = ? ORDER BY timestamp DESC, id DESC LIMIT ?",
            (user_id, today, n),
        ).fetchall()
        if len(rows) < n:
            return None
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(ids))
        self._conn.execute(f"DELETE FROM entries WHERE id IN ({placeholders})", ids)
        self._conn.commit()
        return [r["raw_text"] for r in rows]

    def get_last_entry_items(self, user_id: int) -> list[dict] | None:
        """Return items from the most recent entry today, or None."""
        today = date.today().isoformat()
        row = self._conn.execute(
            "SELECT items_json FROM entries WHERE user_id = ? AND date = ? ORDER BY timestamp DESC, id DESC LIMIT 1",
            (user_id, today),
        ).fetchone()
        return json.loads(row["items_json"]) if row else None

    def recalc_ingredients_from_refs(self, user_id: int, ingredients: list[dict]) -> list[dict]:
        """Recalculate KBJU for reference ingredients using DB values (per 100g)."""
        rows = self._conn.execute(
            "SELECT name, kcal_per_100, protein_per_100, fat_per_100, carbs_per_100 "
            "FROM food_reference WHERE user_id = ?", (user_id,)
        ).fetchall()
        ref_map = {r["name"].strip().lower(): dict(r) for r in rows}
        result = []
        for ing in ingredients:
            if ing.get("source") == "reference":
                ref = ref_map.get(ing.get("name", "").strip().lower())
                if ref:
                    w = float(ing.get("weight_g", 0) or 0)
                    f = w / 100.0
                    ing["kcal"] = round(ref["kcal_per_100"] * f, 1)
                    ing["protein_g"] = round(ref["protein_per_100"] * f, 1)
                    ing["fat_g"] = round(ref["fat_per_100"] * f, 1)
                    ing["carbs_g"] = round(ref["carbs_per_100"] * f, 1)
            result.append(ing)
        return result

    def get_food_references(self, user_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT name, kcal_per_100, protein_per_100, fat_per_100, carbs_per_100 "
            "FROM food_reference WHERE user_id = ? ORDER BY name",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_food_reference_text(self, user_id: int) -> str:
        refs = self.get_food_references(user_id)
        if not refs:
            return ""
        lines = ["Вот проверенные данные пользователя о его продуктах (на 100г):"]
        for r in refs:
            lines.append(
                f"• {r['name']}: {r['kcal_per_100']} ккал, "
                f"{r['protein_per_100']}г б, {r['fat_per_100']}г ж, {r['carbs_per_100']}г у"
            )
        lines.append(
            "ВАЖНО: Используй эти данные В ПЕРВУЮ ОЧЕРЕДЬ. "
            "Если пользователь написал продукт из этого списка, "
            "всегда бери КБЖУ отсюда, а не из общих таблиц."
        )
        return "\n".join(lines)

    # ── History ────────────────────────────────────────────────

    def get_history_days(
        self, user_id: int, limit: int = 5, offset: int = 0
    ) -> list[dict]:
        rows = self._conn.execute(
            """SELECT date,
                     SUM(total_kcal)    AS kcal,
                     SUM(total_protein) AS protein,
                     SUM(total_fat)     AS fat,
                     SUM(total_carbs)   AS carbs,
                     COUNT(*)           AS entries_count
              FROM entries
              WHERE user_id = ?
              GROUP BY date
              ORDER BY date DESC
              LIMIT ? OFFSET ?""",
            (user_id, limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_history_total_days(self, user_id: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(DISTINCT date) AS cnt FROM entries WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return row["cnt"]

    def get_day_entries(self, user_id: int, date_str: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM entries WHERE user_id = ? AND date = ? ORDER BY timestamp, id",
            (user_id, date_str),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self._conn.close()