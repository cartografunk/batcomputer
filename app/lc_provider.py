"""Proveedor de modelos basado en LangChain (se activa con MODEL_BACKEND=langchain).

Mantiene el mismo contrato que agents.ModelProvider: ``call(role, payload) -> dict``,
``last_retries`` y las excepciones AgentFailure / TemporaryProviderFailure, así que el
grafo, el worker y las pruebas existentes no cambian. Lo que agrega:

- Una llamada trazable por agente: con LangSmith activo, cada rol aparece como una
  ejecución hija del grafo con modelo, tokens y latencia.
- Conteo de tokens por modelo en ``usage`` (lo usa la batería de evaluación).
- Modelos distintos por rol con ROLE_MODELS (p. ej. Coder y Reviewer de familias distintas).

No usa el paquete ``langchain`` (init_chat_model) porque exige LangGraph 1.x y el repo fija
LangGraph 0.6; solo usa langchain-core y las integraciones de cada proveedor.
"""

import json
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from langchain_core.exceptions import ModelError, OutputParserException
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables.config import ensure_config
from pydantic import BaseModel, ValidationError
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

from .agents import PROMPTS, AgentFailure, ModelProvider, TemporaryProviderFailure
from .config import Settings
from .state import AnswerOutput, CodeOutput, PlanOutput, ReviewOutput, RouteOutput

ROLE_SCHEMAS: dict[str, type[BaseModel]] = {
    "router": RouteOutput,
    "question": AnswerOutput,
    "planner": PlanOutput,
    "coder": CodeOutput,
    "reviewer": ReviewOutput,
}
ROLES = tuple(ROLE_SCHEMAS)
PROVIDERS = ("remote", "openai", "anthropic", "openrouter", "google", "azure", "azure_responses", "ollama")
GEMINI_OPENAI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
OPENROUTER_URL = "https://openrouter.ai/api/v1"
STRUCTURE_ERROR = "El modelo devolvió una respuesta con estructura inválida."
TEMPORARY_ERROR = "Fallo temporal del proveedor de IA. Tu estado está guardado. Intenta de nuevo."


def azure_output_schema(role: str) -> dict:
    """Translate our Pydantic contract to Azure's supported strict JSON Schema subset.

    Length limits and custom validators remain enforced locally after parsing.
    """
    allowed = {"type", "properties", "required", "items", "anyOf", "$defs", "$ref",
               "enum", "const", "description", "additionalProperties"}

    def clean(value):
        if isinstance(value, list):
            return [clean(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {key: ({name: clean(schema) for name, schema in item.items()}
                        if key in ("properties", "$defs") else clean(item))
                  for key, item in value.items() if key in allowed}
        if result.get("type") == "object":
            result["additionalProperties"] = False
            result["required"] = list(result.get("properties", {}))
        return result

    return {"title": ROLE_SCHEMAS[role].__name__, **clean(ROLE_SCHEMAS[role].model_json_schema())}


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model: str

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"


def parse_model_spec(value: str) -> ModelSpec:
    provider, sep, model = value.strip().partition(":")
    provider, model = provider.strip().lower(), model.strip()
    if not sep or not model or provider not in PROVIDERS:
        raise ValueError(f"Modelo inválido '{value.strip()}'. Use proveedor:modelo con proveedor en {', '.join(PROVIDERS)}.")
    return ModelSpec(provider, model)


def parse_role_models(value: str) -> dict[str, ModelSpec]:
    """Lee ROLE_MODELS: 'router=openai:gpt-5-mini,coder=anthropic:claude-sonnet-5-5'."""
    result = {}
    for item in value.replace(";", ",").split(","):
        if not item.strip():
            continue
        role, sep, target = item.partition("=")
        role = role.strip().lower()
        if not sep or role not in ROLES:
            raise ValueError(f"ROLE_MODELS inválido en '{item.strip()}'. Roles válidos: {', '.join(ROLES)}.")
        result[role] = parse_model_spec(target)
    return result


def default_spec(settings: Settings, role: str) -> ModelSpec:
    """Misma selección de modelo que agents.ModelProvider para no cambiar el comportamiento."""
    gemini_roles = {item.strip() for item in settings.gemini_roles.split(",") if item.strip()}
    if role in gemini_roles:
        return ModelSpec("google", settings.gemini_model_name)
    if settings.model_mode == "ollama":
        return ModelSpec("ollama", settings.ollama_model_name)
    router_override = role == "router" and settings.router_model_name.strip()
    if settings.model_mode in ("azure", "azure_responses"):
        deployment = settings.router_model_name.strip() if router_override else settings.azure_openai_deployment.strip()
        return ModelSpec(settings.model_mode, deployment)
    return ModelSpec("remote", settings.router_model_name.strip() if router_override else settings.model_name)


def resolve_spec(settings: Settings, role: str) -> ModelSpec:
    return parse_role_models(settings.role_models).get(role) or default_spec(settings, role)


def _require(value: str, variable: str) -> str:
    if not value.strip():
        raise AgentFailure(f"Modelo no configurado. Configure {variable} en el servidor.")
    return value.strip()


def build_chat_model(settings: Settings, spec: ModelSpec):
    """Devuelve (chat_model, método de salida estructurada, kwargs extra para with_structured_output)."""
    timeout = settings.model_timeout_seconds
    if spec.provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        model = ChatAnthropic(model=spec.model, api_key=_require(settings.anthropic_api_key, "ANTHROPIC_API_KEY"),
                              max_tokens=settings.max_output_tokens, timeout=timeout, max_retries=0)
        # Salida estructurada nativa (output_config.format). El tool use forzado no está disponible
        # en Sonnet 5.5, Opus 5.5 ni Fable 5.1. Las restricciones que la API no admite (maxLength,
        # etc.) se mueven a la descripción y Pydantic las vuelve a validar al parsear.
        return model, "json_schema", {}

    if spec.provider in ("azure", "azure_responses"):
        from langchain_openai import AzureChatOpenAI
        endpoint = settings.azure_openai_endpoint.strip()
        if not endpoint.startswith("https://") or not spec.model or not settings.azure_openai_api_version.strip():
            raise AgentFailure("Azure OpenAI no configurado. Configure endpoint, deployment y versión de API.")
        model = AzureChatOpenAI(azure_endpoint=endpoint, azure_deployment=spec.model,
                                api_version=settings.azure_openai_api_version.strip(),
                                api_key=_require(settings.model_api_key, "MODEL_API_KEY"),
                                max_tokens=settings.azure_max_completion_tokens, timeout=timeout, max_retries=0,
                                use_responses_api=spec.provider == "azure_responses")
        return model, "json_schema", {"strict": True}

    from langchain_openai import ChatOpenAI
    if spec.provider == "openai":
        base_url, api_key = None, _require(settings.openai_api_key or settings.model_api_key, "OPENAI_API_KEY o MODEL_API_KEY")
    elif spec.provider == "openrouter":
        base_url, api_key = OPENROUTER_URL, _require(settings.openrouter_api_key, "OPENROUTER_API_KEY")
    elif spec.provider == "google":
        base_url, api_key = GEMINI_OPENAI_URL, _require(settings.gemini_api_key, "GEMINI_API_KEY")
    elif spec.provider == "ollama":
        base_url, api_key = settings.ollama_base_url, "ollama"
    else:  # remote: cualquier endpoint compatible con Chat Completions (MODEL_BASE_URL)
        base_url, api_key = settings.model_base_url, _require(settings.model_api_key, "MODEL_API_KEY")

    if base_url and urlparse(base_url).hostname == "openrouter.ai":
        # OpenRouter documenta json_schema; require_parameters evita proveedores que lo ignoran.
        model = ChatOpenAI(model=spec.model, api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0,
                           extra_body={"provider": {"require_parameters": True}})
        return model, "json_schema", {"strict": False}
    model = ChatOpenAI(model=spec.model, api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0)
    return model, "json_mode", {}


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, ModelError):
        return bool(getattr(exc, "is_retryable", False))
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return True
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status == 429 or status >= 500
    names = {cls.__name__ for cls in type(exc).__mro__}
    return bool(names & {"APITimeoutError", "APIConnectionError", "TimeoutException", "ConnectError"})


class LangChainProvider:
    def __init__(self, settings: Settings, model_builder=build_chat_model):
        self.settings = settings
        self.last_retries = 0
        self.usage: dict[str, dict[str, int]] = {}
        self._builder = model_builder
        self._runnables: dict[str, tuple] = {}

    def describe(self) -> dict[str, str]:
        """Modelo efectivo por rol, sin claves. Útil para metadatos de trazas y evaluaciones."""
        described = {}
        for role in ROLES:
            try:
                described[role] = resolve_spec(self.settings, role).label
            except ValueError as exc:
                described[role] = f"error: {exc}"
        return described

    def _runnable(self, role: str):
        if role not in self._runnables:
            try:
                spec = resolve_spec(self.settings, role)
            except ValueError as exc:
                raise AgentFailure(str(exc)) from exc
            model, method, extra = self._builder(self.settings, spec)
            schema = azure_output_schema(role) if spec.provider in ("azure", "azure_responses") else ROLE_SCHEMAS[role]
            runnable = model.with_structured_output(schema, method=method, include_raw=True, **extra)
            self._runnables[role] = (runnable, spec.label)
        return self._runnables[role]

    def _record_usage(self, label: str, message) -> None:
        metadata = getattr(message, "usage_metadata", None) or {}
        entry = self.usage.setdefault(label, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
        entry["calls"] += 1
        entry["input_tokens"] += int(metadata.get("input_tokens") or 0)
        entry["output_tokens"] += int(metadata.get("output_tokens") or 0)

    def call(self, role: str, payload: dict) -> dict:
        self.last_retries = 0
        runnable, label = self._runnable(role)
        messages = [SystemMessage(PROMPTS[role]), HumanMessage(json.dumps(payload, ensure_ascii=False))]
        # Conserva los metadatos heredados del grafo (run_id, conversation_id, nodo) en la traza del modelo.
        inherited = ensure_config().get("metadata") or {}
        config = {"run_name": f"{role}_model", "metadata": {**inherited, "role": role, "model": label}}

        def count_retry(_state):
            self.last_retries += 1

        try:
            for attempt in Retrying(stop=stop_after_attempt(self.settings.model_api_retries + 1),
                                    wait=wait_exponential(multiplier=1, min=1, max=4),
                                    retry=retry_if_exception(is_transient),
                                    before_sleep=count_retry, reraise=True):
                with attempt:
                    result = runnable.invoke(messages, config=config)
        except AgentFailure:
            raise
        except (ValidationError, OutputParserException, json.JSONDecodeError) as exc:
            raise AgentFailure(STRUCTURE_ERROR) from exc
        except Exception as exc:
            if is_transient(exc):
                raise TemporaryProviderFailure(TEMPORARY_ERROR) from exc
            status = getattr(exc, "status_code", None)
            if isinstance(exc, ModelError) or isinstance(status, int):
                suffix = f" (HTTP {status})." if isinstance(status, int) else "."
                raise AgentFailure("El proveedor del modelo rechazó la solicitud" + suffix) from exc
            raise

        self._record_usage(label, result.get("raw"))
        parsed = result.get("parsed")
        if result.get("parsing_error") is not None or parsed is None:
            raise AgentFailure(STRUCTURE_ERROR)
        try:
            return ROLE_SCHEMAS[role].model_validate(parsed).model_dump()
        except (ValidationError, TypeError, ValueError) as exc:
            raise AgentFailure(STRUCTURE_ERROR) from exc


def make_provider(settings: Settings):
    if settings.model_backend == "langchain":
        return LangChainProvider(settings)
    return ModelProvider(settings)
