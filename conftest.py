import os

# Must be set before any app module is imported
os.environ.setdefault("BOT_TOKEN", "1234567890:AAHtest_token_for_tests")
os.environ.setdefault("OPENROUTER_API_KEY", "sk-or-test-0000")
os.environ.setdefault("DB_PATH", ":memory:")
