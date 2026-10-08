"""Pruebas del backend LangChain. No hacen llamadas de red: interceptan el cliente HTTP de los SDK."""

import json
import time

import httpx2
import pytest
from sqlalchemy.orm import sessionmaker

from app.agents import PROMPTS, AgentFailure, ModelProvider, TemporaryProviderFailure
from app.config import Settings as _Settings
from app.db import Base, Conversation, Message, Run, make_engine
from app.lc_provider import (LangChainProvider, ModelSpec, default_spec, is_transient, make_provider,
                             parse_role_models, resolve_spec)
from app.observability import configure_tracing, graph_config
from app.service import claim_next, owner_hash, process_run

def Settings(**values):
    """Settings sin leer .env, para que la configuración local no altere las pruebas."""
    return _Settings(_env_file=None, **values)


ROLE_BY_PROMPT = {prompt: role for role, prompt in PROMPTS.items()}
ANSWERS = {
    "router": {"kind": "NEW_TICKET", "reason": "Nuevo ticket"},
    "question": {"answer": "Respuesta"},
    "planner": {"kind": "code", "summary": "Plan", "criteria": [
        {"description": "Crear app.py", "verification": "Revisar el archivo"}]},
    "coder": {"summary": "Implementado", "files": [{"path": "app.py", "content": "print('ok')\n"}]},
    "reviewer": {"approved": True, "summary": "Cumple", "feedback": []},
}


class FakeHTTP:
    """Sustituye httpx2.Client.send (cliente de los SDK de OpenAI y Anthropic)."""

    def __init__(self, monkeypatch, status=200, content=None, error=None):
        self.requests = []
        self.status, self.content, self.error = status, content, error
        monkeypatch.setattr(httpx2.Client, "send", lambda client, request, **_: self.send(request))
        monkeypatch.setattr(time, "sleep", lambda _: None)

    def send(self, request):
        body = json.loads(request.content or b"{}")
        self.requests.append((str(request.url), body))
        if self.error:
            raise self.error(request)
        if self.status != 200:
            return httpx2.Response(self.status, json={"error": {"message": "fallo", "type": "error"}}, request=request)
        if "anthropic.com" in str(request.url):
            system = body["system"] if isinstance(body["system"], str) else body["system"][0]["text"]
            role = ROLE_BY_PROMPT[system]
            text = self.content if isinstance(self.content, str) else json.dumps(self.content or ANSWERS[role])
            data = {"id": "msg", "type": "message", "role": "assistant", "model": body["model"],
                    "stop_reason": "end_turn", "stop_sequence": None,
                    "content": [{"type": "text", "text": text}],
                    "usage": {"input_tokens": 100, "output_tokens": 20}}
            return httpx2.Response(200, json=data, request=request)
        role = ROLE_BY_PROMPT[body["messages"][0]["content"]]
        content = self.content if isinstance(self.content, str) else json.dumps(self.content or ANSWERS[role])
        data = {"id": "c", "object": "chat.completion", "created": 0, "model": body["model"],
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}
        return httpx2.Response(200, json=data, request=request)


def lc_settings(tmp_path=None, **overrides):
    values = {"model_backend": "langchain", "model_api_key": "test-only", "model_name": "main-model",
              "model_api_retries": 1}
    if tmp_path is not None:
        values["langgraph_sqlite_path"] = str(tmp_path / "cp.sqlite")
    values.update(overrides)
    return Settings(**values)


def test_role_models_parsing_and_errors():
    parsed = parse_role_models("router=openai:gpt-mini; coder = anthropic:claude-sonnet-5-5")
    assert parsed == {"router": ModelSpec("openai", "gpt-mini"), "coder": ModelSpec("anthropic", "claude-sonnet-5-5")}
    for bad in ("tester=openai:x", "coder=foo:x", "coder=openai", "coder"):
        with pytest.raises(ValueError):
            parse_role_models(bad)


def test_default_spec_matches_http_provider_routing():
    assert default_spec(Settings(model_name="m", router_model_name="small"), "router") == ModelSpec("remote", "small")
    assert default_spec(Settings(model_name="m", router_model_name="small"), "coder") == ModelSpec("remote", "m")
    assert default_spec(Settings(gemini_roles="reviewer", gemini_model_name="g"), "reviewer") == ModelSpec("google", "g")
    assert default_spec(Settings(model_mode="ollama", ollama_model_name="o"), "coder") == ModelSpec("ollama", "o")
    azure = Settings(model_mode="azure_responses", azure_openai_deployment="dep")
    assert default_spec(azure, "planner") == ModelSpec("azure_responses", "dep")
    assert resolve_spec(Settings(role_models="coder=anthropic:c"), "coder") == ModelSpec("anthropic", "c")


def test_make_provider_respects_backend():
    assert isinstance(make_provider(Settings()), ModelProvider)
    assert isinstance(make_provider(Settings(model_backend="langchain")), LangChainProvider)


def test_openai_compatible_uses_json_mode_and_counts_tokens(monkeypatch):
    http = FakeHTTP(monkeypatch)
    provider = LangChainProvider(lc_settings(model_base_url="https://api.openai.com/v1"))
    assert provider.call("router", {"message": "x"})["kind"] == "NEW_TICKET"
    url, body = http.requests[-1]
    assert url == "https://api.openai.com/v1/chat/completions"
    assert body["model"] == "main-model"
    assert body["response_format"] == {"type": "json_object"}
    assert provider.usage == {"remote:main-model": {"calls": 1, "input_tokens": 100, "output_tokens": 20}}


def test_openrouter_requests_json_schema_and_required_parameters(monkeypatch):
    http = FakeHTTP(monkeypatch)
    provider = LangChainProvider(lc_settings(role_models="planner=openrouter:anthropic/claude-sonnet-5.5",
                                             openrouter_api_key="or-test"))
    assert provider.call("planner", {"message": "x"})["kind"] == "code"
    url, body = http.requests[-1]
    assert url == "https://openrouter.ai/api/v1/chat/completions"
    assert body["response_format"]["type"] == "json_schema"
    assert body["provider"] == {"require_parameters": True}


def test_anthropic_uses_native_structured_output_and_separate_key(monkeypatch):
    http = FakeHTTP(monkeypatch)
    provider = LangChainProvider(lc_settings(role_models="reviewer=anthropic:claude-sonnet-5-5",
                                             anthropic_api_key="ant-test"))
    assert provider.call("reviewer", {"code": {}})["approved"] is True
    url, body = http.requests[-1]
    assert url == "https://api.anthropic.com/v1/messages"
    schema = body["output_config"]["format"]
    assert schema["type"] == "json_schema" and schema["schema"]["title"] == "ReviewOutput"
    assert "tools" not in body
    assert provider.describe()["reviewer"] == "anthropic:claude-sonnet-5-5"
    assert provider.describe()["coder"] == "remote:main-model"


def test_azure_responses_uses_text_format(monkeypatch):
    sent = []

    def responses(client, request, **kwargs):
        body = json.loads(request.content)
        sent.append((str(request.url), body))
        role = ROLE_BY_PROMPT[body["input"][0]["content"]]
        data = {"id": "r", "object": "response", "created_at": 0, "status": "completed", "model": "dep",
                "output": [{"type": "message", "id": "m", "role": "assistant", "status": "completed",
                            "content": [{"type": "output_text", "text": json.dumps(ANSWERS[role]), "annotations": []}]}],
                "usage": {"input_tokens": 50, "output_tokens": 5, "total_tokens": 55,
                          "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}},
                "parallel_tool_calls": True, "tool_choice": "auto", "tools": []}
        return httpx2.Response(200, json=data, request=request)

    monkeypatch.setattr(httpx2.Client, "send", responses)
    provider = LangChainProvider(lc_settings(model_mode="azure_responses", azure_openai_endpoint="https://ex.openai.azure.com/",
                                             azure_openai_deployment="dep"))
    assert provider.call("router", {"message": "x"})["kind"] == "NEW_TICKET"
    url, body = sent[-1]
    assert url.startswith("https://ex.openai.azure.com/openai/responses")
    assert body["text"]["format"] == {"type": "json_object"}


def test_missing_key_is_explicit():
    provider = LangChainProvider(lc_settings(role_models="coder=anthropic:claude-sonnet-5-5"))
    with pytest.raises(AgentFailure, match="ANTHROPIC_API_KEY"):
        provider.call("coder", {})


def test_invalid_json_and_schema_are_structure_errors(monkeypatch):
    FakeHTTP(monkeypatch, content="no es json")
    with pytest.raises(AgentFailure, match="estructura inválida"):
        LangChainProvider(lc_settings()).call("router", {"message": "x"})
    FakeHTTP(monkeypatch, content={"kind": "OTRA", "reason": "x"})
    with pytest.raises(AgentFailure, match="estructura inválida"):
        LangChainProvider(lc_settings()).call("router", {"message": "x"})


def test_auth_error_is_permanent_and_server_error_is_temporary(monkeypatch):
    http = FakeHTTP(monkeypatch, status=401)
    provider = LangChainProvider(lc_settings())
    with pytest.raises(AgentFailure, match="HTTP 401") as info:
        provider.call("router", {"message": "x"})
    assert not isinstance(info.value, TemporaryProviderFailure)
    assert len(http.requests) == 1

    http = FakeHTTP(monkeypatch, status=503)
    provider = LangChainProvider(lc_settings(model_api_retries=2))
    with pytest.raises(TemporaryProviderFailure):
        provider.call("router", {"message": "x"})
    assert len(http.requests) == 3 and provider.last_retries == 2


def test_timeout_is_temporary(monkeypatch):
    FakeHTTP(monkeypatch, error=lambda request: httpx2.ReadTimeout("lento", request=request))
    with pytest.raises(TemporaryProviderFailure):
        LangChainProvider(lc_settings(model_api_retries=0)).call("router", {"message": "x"})


def test_transient_classification_without_langchain_wrappers():
    class StatusError(Exception):
        def __init__(self, status_code):
            self.status_code = status_code

    assert is_transient(StatusError(429)) and is_transient(StatusError(529))
    assert not is_transient(StatusError(400)) and not is_transient(ValueError("x"))


def test_full_run_with_langchain_backend_and_nested_traces(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    from langchain_core.tracers import LangChainTracer
    from langchain_core.tracers.context import tracing_v2_callback_var

    FakeHTTP(monkeypatch)
    settings = lc_settings(tmp_path)
    engine = make_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    Base.metadata.create_all(engine)
    store = sessionmaker(engine, expire_on_commit=False)
    with store() as db:
        conversation = Conversation(owner_hash=owner_hash("alice"))
        db.add(conversation)
        db.flush()
        message = Message(conversation_id=conversation.id, role="user", content="Crea una app")
        db.add(message)
        db.flush()
        run = Run(conversation_id=conversation.id, message_id=message.id)
        db.add(run)
        db.commit()
        run_id = run.id
    with store() as db:
        _, lease = claim_next(db, settings)
    provider = LangChainProvider(settings)
    # Mismo trazador que usa LangSmith, con un cliente simulado que registra las ejecuciones.
    client = MagicMock()
    token = tracing_v2_callback_var.set(LangChainTracer(client=client, project_name="pruebas"))
    try:
        process_run(store, run_id, settings, provider, lease)
    finally:
        tracing_v2_callback_var.reset(token)
    with store() as db:
        assert db.get(Run, run_id).status == "approved"
    runs = {}
    for call in client.create_run.call_args_list:
        runs[str(call.kwargs["id"])] = call.kwargs
    roots = [run for run in runs.values() if run.get("parent_run_id") is None]
    assert [run["name"] for run in roots] == ["batcomputer"]
    root_id = str(roots[0]["id"])
    assert roots[0]["extra"]["metadata"]["run_id"] == run_id
    by_name = {}
    for run in runs.values():
        by_name.setdefault(run["name"], []).append(run)
        assert str(run["trace_id"]) == root_id
    for role in ("router", "planner", "coder", "reviewer"):
        model_run = by_name[f"{role}_model"][0]
        assert runs[str(model_run["parent_run_id"])]["name"] == role  # nodo del grafo
        assert model_run["extra"]["metadata"]["run_id"] == run_id  # metadatos heredados del grafo
    assert len(by_name["ChatOpenAI"]) == 4
    assert sum(entry["calls"] for entry in provider.usage.values()) == 4


def test_configure_tracing_exports_without_overriding(monkeypatch):
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.setenv("LANGSMITH_PROJECT", "definido-en-railway")
    assert configure_tracing(Settings(langsmith_tracing=False)) is False
    assert configure_tracing(Settings(langsmith_tracing=True)) is False
    assert configure_tracing(Settings(langsmith_tracing=True, langsmith_api_key="ls-test", langsmith_project="otro"))
    import os
    assert os.environ["LANGSMITH_TRACING"] == "true"
    assert os.environ["LANGSMITH_PROJECT"] == "definido-en-railway"
    config = graph_config(Settings(), "run-1", "conv-1", ModelProvider(Settings()))
    assert config["configurable"] == {"thread_id": "run-1"} and "models" not in config["metadata"]
