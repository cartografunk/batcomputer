from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_env: str = "development"
    database_url: str = "sqlite:///./local.db"
    auth_tokens: str = ""
    model_api_key: str = ""
    model_base_url: str = "https://api.openai.com/v1"
    model_name: str = "gpt-4.1-mini"
    max_coder_attempts: int = 3
    max_job_recoveries: int = 2
    max_concurrent_runs: int = 2
    max_queued_runs_per_owner: int = 10
    model_api_retries: int = 2
    model_timeout_seconds: float = 30
    job_stale_seconds: int = 180
    worker_poll_seconds: float = 1
    rate_limit_per_hour: int = 30
    cors_origins: str = "http://localhost:8000"
    e2b_api_key: str = ""
    sandbox_timeout_seconds: int = 45
    sandbox_output_chars: int = 4000
    frontend_dir: str = "app/static"
    shutdown_grace_seconds: int = 10


settings = Settings()
