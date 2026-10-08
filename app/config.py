from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_env: str = "development"
    database_url: str = "sqlite:///./local.db"
    langgraph_sqlite_path: str = "./langgraph_state.sqlite"
    auth_tokens: str = ""
    model_api_key: str = ""
    model_base_url: str = "https://api.openai.com/v1"
    model_name: str = "gpt-4.1-mini"
    router_model_name: str = ""
    model_mode: Literal["remote", "azure", "azure_responses", "ollama"] = "remote"
    azure_openai_endpoint: str = ""
    azure_openai_api_version: str = "2025-04-01-preview"
    azure_openai_deployment: str = ""
    azure_max_completion_tokens: int = Field(default=16384, ge=1)
    ollama_base_url: str = "http://127.0.0.1:11434/v1"
    ollama_model_name: str = "deepseek-coder"
    gemini_api_key: str = ""
    gemini_model_name: str = "gemini-3.8-flash"
    gemini_roles: str = ""
    tavily_api_key: str = ""
    tavily_max_results: int = 3
    tavily_timeout_seconds: float = 10
    max_coder_attempts: int = Field(default=3, ge=1, le=3)
    max_job_recoveries: int = 2
    max_concurrent_runs: int = 2
    max_queued_runs_per_owner: int = 10
    model_api_retries: int = 2
    model_timeout_seconds: float = 120
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
