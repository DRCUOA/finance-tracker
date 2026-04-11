from pydantic_settings import BaseSettings

APP_VERSION = "5.4.0"


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql+asyncpg://finance:finance@localhost:5433/finance_tracker"
    SECRET_KEY: str = "change-me-to-a-long-random-string"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    ALGORITHM: str = "HS256"

    AKAHU_BASE_URL: str = "https://api.akahu.io/v1"
    AKAHU_APP_TOKEN: str = ""
    AKAHU_USER_TOKEN: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
