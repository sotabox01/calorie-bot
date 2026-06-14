from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    bot_token: str
    openrouter_api_key: str
    owner_id: int = 518283574
    model: str = "google/gemini-2.5-flash-lite"
    db_path: str = "data/calorie_bot.db"
    log_level: str = "INFO"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
