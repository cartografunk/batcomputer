from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_env: str = "development"
    database_url: str = "sqlite:///./local.db"
    langgraph_sqlite_path: str = "./langgraph_state.sqlite"
    auth_tokens: str = ""
    auth_mode: Literal["legacy", "supabase"] = "legacy"
    supabase_url: str = ""
    supabase_publishable_key: str = ""
    allowed_user_emails: str = "cartografunk@gmail.com,iramjuarez98@hotmail.com"
    guest_rate_limit_per_hour: int = Field(default=3, ge=0)
    guest_global_rate_limit_per_hour: int = Field(default=10, ge=0)
    model_api_key: str = ""
    model_base_url: str = "https://api.openai.com/v1"
    model_name: str = "gpt-4.1-mini"
    router_model_name: str = ""
    model_mode: Literal["remote", "azure", "azure_responses", "ollama"] = "remote"
    # "http" conserva el cliente original; "langchain" usa app/lc_provider.py
    # (tokens por llamada, trazas LangSmith por agente y modelos distintos por rol).
    model_backend: Literal["http", "langchain"] = "http"
    # Solo backend langchain. Formato: "router=openai:gpt-5-mini,reviewer=anthropic:claude-sonnet-5-5".
    # Los roles sin entrada usan MODEL_MODE/MODEL_NAME como hasta ahora.
    role_models: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    openrouter_api_key: str = ""
    max_output_tokens: int = Field(default=16384, ge=1)
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
    scout_enabled: bool = True
    scout_timeout_seconds: float = 8
    max_coder_attempts: int = Field(default=3, ge=1, le=3)
    max_job_recoveries: int = 2
    max_concurrent_runs: int = 2
    max_queued_runs_per_owner: int = 10
    model_api_retries: int = 2
    model_timeout_seconds: float = 120
    job_stale_seconds: int = 180
    worker_poll_seconds: float = 1
    rate_limit_per_hour: int = 30
    global_rate_limit_per_hour: int = 100
    cors_origins: str = "http://localhost:8000"
    e2b_api_key: str = ""
    sandbox_timeout_seconds: int = 45
    sandbox_output_chars: int = 4000
    frontend_dir: str = "app/static"
    shutdown_grace_seconds: int = 10
    # Observabilidad con LangSmith (desactivada por defecto; ver docs/EVALS.md).
    langsmith_tracing: bool = False
    langsmith_api_key: str = ""
    langsmith_project: str = "batcomputer"
    langsmith_endpoint: str = ""
    # Modelo juez de la batería de evaluación, "proveedor:modelo". Vacío = sin juez.
    judge_model: str = ""


settings = Settings()
