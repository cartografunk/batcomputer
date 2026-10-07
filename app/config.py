from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "sqlite:///./local.db"
    auth_tokens: str = ""
    model_api_key: str = ""
    model_base_url: str = "https://api.openai.com/v1"
    model_name: str = "gpt-4.1-mini"
    max_coder_attempts: int = 3
    model_api_retries: int = 2
    model_timeout_seconds: float = 30
    job_stale_seconds: int = 180
    worker_poll_seconds: float = 1
    rate_limit_per_hour: int = 30
    cors_origins: str = "http://localhost:8000"


settings = Settings()
