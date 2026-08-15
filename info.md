# CalorieBot — Deployment Guide

## Overview
Telegram-бот для учёта калорий через OpenRouter LLM (Gemini 2.5 Flash Lite).
Стек: Python 3, `python-telegram-bot`, SQLite, Docker-не требуется.

## Connect to VPS
```bash
ssh caloriebot-vps
# or
ssh -p 25565 radeonovich@144.31.164.138
```
Key auth is set up. No password needed.

## Deploy
```bash
# Copy files to VPS
scp -P 25565 bot.py db.py radeonovich@144.31.164.138:~/calorie-bot/

# Restart the bot
ssh caloriebot-vps "
  screen -S caloriebot -X quit 2>/dev/null;
  sleep 3 &&
  screen -dmS caloriebot bash -c 'cd ~/calorie-bot && exec ~/calorie-bot/venv/bin/python bot.py' &&
  sleep 2 &&
  screen -ls"

# Verify
ssh caloriebot-vps "ps aux | grep bot.py | grep -v grep"
```

IMPORTANT: The bot runs under `~/calorie-bot/venv/bin/python`, NOT system `python3`.
Wait 3-6s between kill and restart (Telegram long-poll cooldown).

## Files
- `bot.py` — main bot logic
- `db.py` — SQLite database
- `config.py` — pydantic settings (bot token, db path, OpenRouter key, owner_id)
- `llm_parser.py` — LLM integration
- `recipe_prompt.py` — recipe parsing prompt

## Database
SQLite at path configured in `config.py` via `settings.db_path`.

## Git
```bash
cd ~/repos/calorie-bot
git push origin main
# If push hangs: may need to choose GitHub user in browser modal first.
# Until then: deploy via SCP (see above).
```

## User Profile
- Owner ID: 518283574
- Russian-speaking
- Prefers clear, concise responses
- Uses Telegram for bot interaction
- Cost-sensitive about LLM API usage

## LLM
- Provider: OpenRouter
- Model: Gemini 2.5 Flash Lite
- `/import` parses user-provided KBJU data into `food_reference` table
- `/recipe` parses ingredients + cooked weight → per-100g KBJU
- All reference ingredients recalculated server-side from DB (NOT trusted from LLM)

## Key Design Decisions
- Grains default to COOKED (варёные), not dry
- `/recipe` does NOT auto-save anything. User must `/save <name>` to create reference
- `recalc_ingredients_from_refs()` in bot.py recalculates all reference ingredients server-side
- No nagging/ворчание system
- `pending_recipe` stored in `user_settings` table (JSON blob), cleared on `/save`

## Recent Changes
- Removed nagging system entirely
- Changed `/recipe` + `/save` to pending_recipe flow (no auto-save)
- Added server-side recalculation of reference ingredients
- Added `/settings` with hide_nutrients toggle