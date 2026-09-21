from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STORE_", env_file=".env", extra="ignore")

    database_url: str = f"sqlite:///{BASE_DIR / 'store.db'}"
    seed_file: Path = BASE_DIR / "data" / "mock_data.json"


settings = Settings()
