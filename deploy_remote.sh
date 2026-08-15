#!/usr/bin/env bash
set -e
git -C ~/calorie-bot pull origin main
screen -S caloriebot -X quit 2>/dev/null; sleep 3
screen -dmS caloriebot bash -c 'cd ~/calorie-bot; exec ~/calorie-bot/venv/bin/python bot.py'
sleep 2
screen -ls
