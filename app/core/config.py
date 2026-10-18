from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve absolute path to project root .env file so it loads regardless of working directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
ENV_FILE = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    """
    Application settings model that reads configuration from environment variables or .env file.
    """
    DATABASE_URL: str = ""
    JWT_SECRET: str = ""
    JWT_ACCESS_SECRET: str = ""
    JWT_ALGORITHM: str = "HS256"
    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    RAZORPAY_WEBHOOK_SECRET: str = ""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore"
    )

    @property
    def jwt_secrets(self) -> list[str]:
        """Returns all populated JWT signing secrets to support local and deployed Auth Service tokens."""
        return [s for s in [self.JWT_SECRET, self.JWT_ACCESS_SECRET] if s]


# Reads and caches settings from the .env file.
# Caching avoids reading from disk or environment repeatedly on every request.
@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
