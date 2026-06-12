from pydantic_settings import BaseSettings

APP_VERSION = "5.6.0"

# The shipped placeholder. Booting with this (or any too-short key) is refused
# by validate_security_config() so an insecurely-configured instance can't run.
DEFAULT_SECRET_KEY = "change-me-to-a-long-random-string"
MIN_SECRET_KEY_LENGTH = 32


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql+asyncpg://finance:finance@localhost:5433/finance_tracker"
    SECRET_KEY: str = DEFAULT_SECRET_KEY
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    ALGORITHM: str = "HS256"

    # Set Secure on session cookies. Default True (production over HTTPS); set
    # COOKIE_SECURE=false in .env for local http://localhost development.
    COOKIE_SECURE: bool = True

    AKAHU_BASE_URL: str = "https://api.akahu.io/v1"
    AKAHU_APP_TOKEN: str = ""
    AKAHU_USER_TOKEN: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()


def validate_security_config(s: "Settings" = settings) -> None:
    """Refuse to boot with an insecure SECRET_KEY.

    Raised at app startup (see app.main lifespan), not at import, so tooling and
    tests that merely import the settings object aren't forced to provide a key.
    """
    if s.SECRET_KEY == DEFAULT_SECRET_KEY or len(s.SECRET_KEY) < MIN_SECRET_KEY_LENGTH:
        raise RuntimeError(
            "SECRET_KEY must be set to a strong, non-default value of at least "
            f"{MIN_SECRET_KEY_LENGTH} characters. Refusing to start."
        )
