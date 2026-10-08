"""Trazas con LangSmith.

El SDK de LangSmith lee variables de entorno del proceso, pero Settings carga `.env` sin
exportarlo. configure_tracing() copia la configuración a os.environ (sin pisar valores ya
definidos, p. ej. en Railway) para que LangGraph y LangChain envíen las trazas.

Con LANGSMITH_TRACING desactivado no se envía nada. Las trazas contienen tickets y código
generado; no contienen claves de API.
"""

import logging
import os

from .config import Settings

logger = logging.getLogger(__name__)


def configure_tracing(settings: Settings) -> bool:
    if not settings.langsmith_tracing:
        return False
    api_key = settings.langsmith_api_key.strip() or os.environ.get("LANGSMITH_API_KEY", "")
    if not api_key:
        logger.warning("LANGSMITH_TRACING está activo pero falta LANGSMITH_API_KEY; no se enviarán trazas.")
        return False
    os.environ.setdefault("LANGSMITH_API_KEY", api_key)
    os.environ.setdefault("LANGSMITH_TRACING", "true")
    os.environ.setdefault("LANGSMITH_PROJECT", settings.langsmith_project)
    if settings.langsmith_endpoint.strip():
        os.environ.setdefault("LANGSMITH_ENDPOINT", settings.langsmith_endpoint.strip())
    return True


def graph_config(settings: Settings, run_id: str, conversation_id: str, provider) -> dict:
    """Config de graph.invoke: checkpoint por run y metadatos para buscar la traza en LangSmith."""
    describe = getattr(provider, "describe", None)
    metadata = {"run_id": run_id, "conversation_id": conversation_id, "backend": settings.model_backend}
    if callable(describe):
        metadata["models"] = describe()
    return {"configurable": {"thread_id": run_id}, "run_name": "batcomputer", "tags": ["batcomputer"],
            "metadata": metadata}
