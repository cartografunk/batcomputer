from datetime import timedelta
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.agents import AgentFailure, ModelProvider, TavilyResearcher, TemporaryProviderFailure, build_graph
from app.config import Settings
from app.db import Base, Conversation, FileVersion, Message, Review, Run, Trace, make_engine, now
from app.service import claim_next, latest_files, owner_hash, process_run
from app.state import PlanOutput, WorkflowState


class FakeProvider:
    def __init__(self, answers):
        self.answers = iter(answers)
        self.calls = []

    def call(self, role, payload):
        self.calls.append((role, payload))
        if role == "router":
            kind = "QUESTION" if "?" in payload["message"] or payload["message"].startswith("What ") else (
                "MODIFICATION" if payload["has_previous_files"] else "NEW_TICKET")
            return {"kind": kind, "reason": "Simulated route"}
        result = next(self.answers)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def store(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def config(tmp_path):
    return Settings(auth_tokens="alice,bob", max_coder_attempts=3,
                    langgraph_sqlite_path=str(tmp_path / "checkpoints.sqlite"))


def queued(store, owner="alice", conversation=None, content="Build app"):
    with store() as db:
        if conversation is None:
            conversation = Conversation(owner_hash=owner_hash(owner))
            db.add(conversation)
            db.flush()
        message = Message(conversation_id=conversation.id, role="user", content=content)
        db.add(message)
        db.flush()
        run = Run(conversation_id=conversation.id, message_id=message.id)
        db.add(run)
        db.commit()
        return conversation.id, run.id


PLAN = {"kind": "code", "summary": "Create app", "criteria": [
    {"description": "Provide app.py", "verification": "Inspect generated file"}]}
CODE = {"summary": "Implemented app", "files": [{"path": "app.py", "content": "print('v1')"}]}
YES = {"approved": True, "summary": "Looks good", "feedback": []}
NO = {"approved": False, "summary": "Needs change", "feedback": ["Fix output"]}


def execute(store, config, run_id, fake):
    with store() as db:
        claimed, lease = claim_next(db, config)
        assert claimed == run_id
    process_run(store, run_id, config, fake, lease)
    with store() as db:
        return db.get(Run, run_id)


def test_initial_approval(store, config):
    _, run_id = queued(store)
    run = execute(store, config, run_id, FakeProvider([PLAN, CODE, YES]))
    assert (run.status, run.attempts) == ("approved", 1)
    with store() as db:
        assert len(db.scalars(select(Review)).all()) == 1
        assert len(db.scalars(select(FileVersion)).all()) == 2


def test_rejection_correction_approval(store, config):
    _, run_id = queued(store)
    revised = {"summary": "Fixed", "files": [{"path": "app.py", "content": "print('v2')"}]}
    fake = FakeProvider([PLAN, CODE, NO, revised, YES])
    run = execute(store, config, run_id, fake)
    assert (run.status, run.attempts) == ("approved", 2)
    assert fake.calls[4][1]["review_feedback"] == ["Fix output"]
    with store() as db:
        assert [f.attempt for f in db.scalars(select(FileVersion).order_by(FileVersion.attempt)).all()] == [1, 2, 2]
        assert {f["path"] for f in latest_files(db, run.conversation_id)} == {"app.py", "README.md"}


def test_exhaustion(store, config):
    _, run_id = queued(store)
    run = execute(store, config, run_id, FakeProvider([PLAN, CODE, NO, CODE, NO, CODE, NO]))
    assert (run.status, run.attempts) == ("exhausted", 3)
    assert run.result == "Se alcanzó el límite de intentos de revisión. Error persistente detectado."


def test_schema_requires_verifiable_criteria():
    with pytest.raises(ValueError):
        PlanOutput.model_validate({"kind": "code", "summary": "Incomplete", "criteria": []})
    with pytest.raises(ValueError):
        WorkflowState.model_validate({"message": "", "history": []})


def test_syntax_failure_forces_rejection_without_llm_review(store, config):
    _, run_id = queued(store)
    broken = {"summary": "Broken", "files": [{"path": "app.py", "content": "def broken(:\n"}]}
    fake = FakeProvider([PLAN, broken, CODE, YES])
    run = execute(store, config, run_id, fake)
    assert run.status == "approved"
    assert run.attempts == 2
    assert fake.calls[3][0] == "coder"
    assert "invalid syntax" in " ".join(fake.calls[3][1]["review_feedback"])


def test_provider_failure_pauses_and_can_requeue(store, config, monkeypatch):
    import app.main as main
    _, run_id = queued(store)
    paused = execute(store, config, run_id, FakeProvider([
        TemporaryProviderFailure("Fallo temporal del proveedor de IA. Tu estado está guardado. Intenta de nuevo.")]))
    assert paused.status == "paused"
    assert "estado está guardado" in paused.result
    assert (config.langgraph_sqlite_path and __import__("pathlib").Path(config.langgraph_sqlite_path).exists())
    monkeypatch.setattr(main, "SessionLocal", store)
    monkeypatch.setattr(main.settings, "auth_tokens", "alice,bob")
    response = TestClient(main.app).post(f"/api/runs/{run_id}/retry", headers={"Authorization": "Bearer alice"})
    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    with store() as db:
        _, new_lease = claim_next(db, config)
    process_run(store, run_id, config, FakeProvider([PLAN, CODE, YES]), new_lease)
    with store() as db:
        assert db.get(Run, run_id).status == "approved"


def test_sse_streams_agent_events_and_enforces_owner(store, config, monkeypatch):
    import app.main as main
    conversation, run_id = queued(store)
    execute(store, config, run_id, FakeProvider([PLAN, CODE, YES]))
    monkeypatch.setattr(main, "SessionLocal", store)
    monkeypatch.setattr(main.settings, "auth_tokens", "alice,bob")
    client = TestClient(main.app)
    url = f"/stream/{conversation}?run_id={run_id}"
    assert client.get(url, headers={"Authorization": "Bearer bob"}).status_code == 404
    response = client.get(url, headers={"Authorization": "Bearer alice"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"node": "Router"' in response.text
    assert '"node": "Documenter"' in response.text


def test_anonymous_session_owns_conversation_and_respects_global_limit(store, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "SessionLocal", store)
    monkeypatch.setattr(main.settings, "auth_tokens", "test-signing-secret")
    monkeypatch.setattr(main.settings, "global_rate_limit_per_hour", 1)
    visitor = TestClient(main.app)
    session = visitor.get("/api/session")
    assert session.status_code == 200
    assert session.headers["cache-control"] == "no-store"
    assert "httponly" in session.headers["set-cookie"].lower()
    conversation = visitor.post("/api/conversations")
    assert conversation.status_code == 201
    conversation_id = conversation.json()["id"]
    assert visitor.get(f"/api/conversations/{conversation_id}").status_code == 200

    other = TestClient(main.app)
    assert other.get(f"/api/conversations/{conversation_id}").status_code == 401
    other.get("/api/session")
    assert other.get(f"/api/conversations/{conversation_id}").status_code == 404
    assert visitor.post(f"/api/conversations/{conversation_id}/messages", json={"content": "Ticket"}).status_code == 202
    assert visitor.post(f"/api/conversations/{conversation_id}/messages", json={"content": "Otro"}).status_code == 429


def test_ollama_mode_needs_no_remote_api_key(monkeypatch):
    import httpx
    import app.agents as agents

    seen = {}
    class ChatClient:
        def __init__(self, **_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def post(self, url, **kwargs):
            seen.update(url=url, **kwargs)
            return httpx.Response(200, json={"choices": [{"message": {"content":
                '{"kind":"NEW_TICKET","reason":"new work"}'}}]},
                request=httpx.Request("POST", url))

    monkeypatch.setattr(agents.httpx, "Client", ChatClient)
    provider = ModelProvider(Settings(model_mode="ollama", ollama_model_name="deepseek-coder"))
    assert provider.call("router", {"message": "Build"})["kind"] == "NEW_TICKET"
    assert seen["url"] == "http://127.0.0.1:11434/v1/chat/completions"
    assert seen["json"]["model"] == "deepseek-coder"


@pytest.mark.parametrize("bad", [{"summary": "missing kind"}, AgentFailure("El proveedor del modelo no respondió correctamente o agotó los reintentos.")])
def test_malformed_or_timeout(store, config, bad):
    _, run_id = queued(store)
    run = execute(store, config, run_id, FakeProvider([bad]))
    assert run.status == "technical_error"
    assert run.attempts == 0
    assert "API" not in run.error


def test_followup_uses_previous_code(store, config):
    conversation, first = queued(store)
    execute(store, config, first, FakeProvider([PLAN, CODE, YES]))
    _, second = queued(store, conversation=Conversation(id=conversation, owner_hash=owner_hash("alice")), content="Add input")
    modified = {"summary": "Added input", "files": [{"path": "app.py", "content": "input()"}]}
    fake = FakeProvider([PLAN, modified, YES])
    run = execute(store, config, second, fake)
    assert run.status == "approved"
    assert {f["path"] for f in fake.calls[1][1]["files"]} == {"app.py", "README.md"}
    assert {f["path"] for f in fake.calls[2][1]["previous_files"]} == {"app.py", "README.md"}


def test_question_does_not_reimplement(store, config):
    conversation, first = queued(store)
    execute(store, config, first, FakeProvider([PLAN, CODE, YES]))
    _, second = queued(store, conversation=Conversation(id=conversation, owner_hash=owner_hash("alice")), content="What does it do?")
    fake = FakeProvider([{"answer": "It prints v1."}])
    run = execute(store, config, second, fake)
    assert run.status == "answered"
    assert [role for role, _ in fake.calls] == ["router", "question"]


def test_separation_and_interrupted_recovery(store, config):
    a, first = queued(store, "alice")
    b, second = queued(store, "bob")
    with store() as db:
        run = db.get(Run, first)
        run.status = "running"
        run.heartbeat_at = now() - timedelta(seconds=config.job_stale_seconds + 1)
        db.commit()
    with store() as db:
        claimed, _ = claim_next(db, config)
        assert claimed in (first, second)
        assert db.get(Run, first).status in ("queued", "running")
        assert a != b


def test_access_control(store, config, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "SessionLocal", store)
    monkeypatch.setattr(main.settings, "auth_tokens", "alice,bob")
    conversation, run_id = queued(store, "alice")
    client = TestClient(main.app)
    bob = {"Authorization": "Bearer bob"}
    assert client.get(f"/api/conversations/{conversation}", headers=bob).status_code == 404
    assert client.get(f"/api/runs/{run_id}", headers=bob).status_code == 404
    assert client.get(f"/api/runs/{run_id}/traces", headers=bob).status_code == 404
    assert client.get(f"/api/runs/{run_id}/files", headers=bob).status_code == 404


def test_http_async_round_trip(store, config, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "SessionLocal", store)
    monkeypatch.setattr(main.settings, "auth_tokens", "alice,bob")
    monkeypatch.setattr(main.settings, "worker_poll_seconds", 0.01)
    real_process = process_run
    monkeypatch.setattr(main, "process_run", lambda factory, run_id, settings, lease_id=None:
                        real_process(factory, run_id, settings, FakeProvider([PLAN, CODE, YES]), lease_id))
    with TestClient(main.app) as client:
        headers = {"Authorization": "Bearer alice"}
        conversation = client.post("/api/conversations", headers=headers).json()["id"]
        response = client.post(f"/api/conversations/{conversation}/messages", headers=headers,
                               json={"content": "Build app"})
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        for _ in range(100):
            result = client.get(f"/api/runs/{run_id}", headers=headers).json()
            if result["status"] == "approved":
                break
            time.sleep(0.05)
        assert result["status"] == "approved"
        assert result["validation_status"] == "not_executed"
        assert len(client.get(f"/api/runs/{run_id}/traces", headers=headers).json()) == 6
        assert client.get(f"/api/runs/{run_id}/files", headers=headers).json()[0]["content"] == "print('v1')"


def test_old_lease_cannot_write(store, config):
    _, run_id = queued(store)
    with store() as db:
        claimed, old_lease = claim_next(db, config)
        assert claimed == run_id
        run = db.get(Run, run_id)
        run.heartbeat_at = now() - timedelta(seconds=config.job_stale_seconds + 1)
        db.commit()
    with store() as db:
        _, new_lease = claim_next(db, config)
    assert old_lease != new_lease
    process_run(store, run_id, config, FakeProvider([PLAN, CODE, YES]), old_lease)
    with store() as db:
        assert db.get(Run, run_id).status == "running"
        assert db.scalars(select(Trace)).all() == []


def test_same_conversation_runs_are_serial(store, config):
    conversation, first = queued(store)
    _, second = queued(store, conversation=Conversation(id=conversation, owner_hash=owner_hash("alice")))
    with store() as db:
        assert claim_next(db, config)[0] == first
    with store() as db:
        assert claim_next(db, config) is None
    execute_fake = FakeProvider([PLAN, CODE, YES])
    with store() as db:
        old_lease = db.get(Run, first).lease_id
    process_run(store, first, config, execute_fake, old_lease)
    with store() as db:
        assert claim_next(db, config)[0] == second


def test_model_timeout_counts_api_retries(monkeypatch):
    import httpx
    import app.agents as agents

    class TimeoutClient:
        def __init__(self, **_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def post(self, *_args, **_kwargs):
            raise httpx.ReadTimeout("request timed out")

    monkeypatch.setattr(agents.httpx, "Client", TimeoutClient)
    monkeypatch.setattr(agents.time, "sleep", lambda _: None)
    provider = ModelProvider(Settings(model_api_key="test-only", model_api_retries=2))
    with pytest.raises(AgentFailure, match="Fallo temporal"):
        provider.call("planner", {"message": "Hello"})
    assert provider.last_retries == 2


def test_research_is_requested_once_and_grounded():
    config = Settings(tavily_api_key="test-only")
    first = {"kind": "code", "summary": "Check current API", "criteria": PLAN["criteria"],
             "research_query": "current library API"}
    final = {"kind": "code", "summary": "Use documented API", "criteria": PLAN["criteria"],
             "research_query": "search again"}
    fake = FakeProvider([first, final, CODE, YES])

    class FakeResearcher:
        calls = []

        def search(self, query):
            self.calls.append(query)
            return [{"title": "Docs", "url": "https://example.com/docs", "content": "API details"}]

    researcher = FakeResearcher()
    traces = []
    class Validator:
        def validate(self, _files):
            return {"status": "not_executed", "summary": "No sandbox", "checks": [], "duration_ms": 0}

    graph = build_graph(fake, config, lambda *args: traces.append(args), Validator(), researcher)
    result = graph.invoke({"message": "Build against the latest API", "history": [], "previous_files": []})
    assert result["status"] == "approved"
    assert researcher.calls == ["current library API"]
    assert fake.calls[2][1]["research_results"][0]["url"] == "https://example.com/docs"
    assert [trace[0] for trace in traces] == ["router", "planner", "research", "planner", "coder", "validator", "reviewer", "documenter"]


def test_question_uses_dedicated_node():
    config = Settings()
    fake = FakeProvider([{"answer": "See the existing files."}])
    class Validator:
        def validate(self, _files):
            raise AssertionError("question should not generate code")
    graph = build_graph(fake, config, lambda *_: None, Validator())
    result = graph.invoke({"message": "What is current?", "history": [], "previous_files": []})
    assert result["status"] == "answered"
    assert result["result"] == "See the existing files."


def test_tavily_bounds_results_and_uses_bearer(monkeypatch):
    import app.agents as agents

    seen = {}
    class SearchClient:
        def __init__(self, **_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def post(self, url, **kwargs):
            seen.update(url=url, **kwargs)
            return httpx.Response(200, json={"results": [
                {"title": "Docs", "url": "https://example.com", "content": "x" * 3000},
                {"title": "Unsafe", "url": "file:///private", "content": "skip"},
            ]}, request=httpx.Request("POST", url))

    import httpx
    monkeypatch.setattr(agents.httpx, "Client", SearchClient)
    config = Settings(tavily_api_key="test-only", tavily_max_results=20)
    results = TavilyResearcher(config).search("what changed")
    assert seen["url"] == "https://api.tavily.com/search"
    assert seen["headers"]["Authorization"] == "Bearer test-only"
    assert seen["json"]["max_results"] == 5
    assert len(results) == 1
    assert len(results[0]["content"]) == 1500


def test_gemini_compatible_endpoint_configuration(monkeypatch):
    import httpx
    import app.agents as agents

    seen = {}
    class ChatClient:
        def __init__(self, **_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def post(self, url, **kwargs):
            seen.update(url=url, **kwargs)
            return httpx.Response(200, json={"choices": [{"message": {"content":
                '{"kind":"question","summary":"ok","answer":"ok"}'}}]},
                request=httpx.Request("POST", url))

    monkeypatch.setattr(agents.httpx, "Client", ChatClient)
    provider = ModelProvider(Settings(model_api_key="test-only", model_name="gemini-3.8-flash",
        model_base_url="https://generativelanguage.googleapis.com/v1beta/openai/"))
    assert provider.call("planner", {"message": "Hello"})["kind"] == "question"
    assert seen["url"] == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    assert seen["json"]["model"] == "gemini-3.8-flash"


def test_gemini_reviewer_can_use_separate_key(monkeypatch):
    import httpx
    import app.agents as agents

    calls = []
    class ChatClient:
        def __init__(self, **_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]},
                                  request=httpx.Request("POST", url))

    monkeypatch.setattr(agents.httpx, "Client", ChatClient)
    config = Settings(model_api_key="openai-test", gemini_api_key="gemini-test",
                      gemini_roles="reviewer")
    provider = ModelProvider(config)
    provider.call("planner", {})
    provider.call("reviewer", {})
    assert calls[0][0] == "https://api.openai.com/v1/chat/completions"
    assert calls[0][1]["headers"]["Authorization"] == "Bearer openai-test"
    assert calls[1][0] == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    assert calls[1][1]["headers"]["Authorization"] == "Bearer gemini-test"


def test_router_can_use_a_smaller_model_with_same_key(monkeypatch):
    import httpx
    import app.agents as agents

    models = []
    class ChatClient:
        def __init__(self, **_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def post(self, url, **kwargs):
            models.append(kwargs["json"]["model"])
            return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]},
                                  request=httpx.Request("POST", url))

    monkeypatch.setattr(agents.httpx, "Client", ChatClient)
    provider = ModelProvider(Settings(model_api_key="test-only", model_name="gpt-6.1-sol",
                                      router_model_name="some-nano"))
    provider.call("router", {})
    provider.call("planner", {})
    assert models == ["some-nano", "gpt-6.1-sol"]


def test_azure_openai_uses_deployment_route_and_api_key(monkeypatch):
    import httpx
    import app.agents as agents

    calls = []
    class ChatClient:
        def __init__(self, **_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]},
                                  request=httpx.Request("POST", url))

    monkeypatch.setattr(agents.httpx, "Client", ChatClient)
    provider = ModelProvider(Settings(model_mode="azure", model_api_key="azure-test",
        azure_openai_endpoint="https://open-ai-exam.openai.azure.com/",
        azure_openai_deployment="gpt-5-mini", azure_openai_api_version="2024-12-01-preview",
        router_model_name="gpt-5-nano"))
    provider.call("planner", {})
    provider.call("router", {})
    assert calls[0][0] == "https://open-ai-exam.openai.azure.com/openai/deployments/gpt-5-mini/chat/completions"
    assert calls[0][1]["headers"] == {"api-key": "azure-test"}
    assert calls[0][1]["params"] == {"api-version": "2024-12-01-preview"}
    assert calls[0][1]["json"]["model"] == "gpt-5-mini"
    assert calls[0][1]["json"]["max_completion_tokens"] == 16384
    assert calls[1][0].endswith("/deployments/gpt-5-nano/chat/completions")
    assert calls[1][1]["json"]["model"] == "gpt-5-nano"


def test_azure_openai_requires_deployment():
    provider = ModelProvider(Settings(model_mode="azure", model_api_key="azure-test",
                                      azure_openai_endpoint="https://example.openai.azure.com/"))
    with pytest.raises(AgentFailure, match="Azure OpenAI no configurado"):
        provider.call("planner", {})


def test_azure_responses_uses_shared_endpoint_and_extracts_output(monkeypatch):
    import httpx
    import app.agents as agents

    seen = {}
    class ResponsesClient:
        def __init__(self, **_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def post(self, url, **kwargs):
            seen.update(url=url, **kwargs)
            data = {"status": "completed", "output": [{"type": "message", "content": [
                {"type": "output_text", "text": '{"kind":"QUESTION","reason":"test"}'}]}]}
            return httpx.Response(200, json=data, request=httpx.Request("POST", url))

    monkeypatch.setattr(agents.httpx, "Client", ResponsesClient)
    provider = ModelProvider(Settings(model_mode="azure_responses", model_api_key="azure-test",
        azure_openai_endpoint="https://open-ai-exam.openai.azure.com/",
        azure_openai_deployment="gpt-5-mini", azure_openai_api_version="2025-04-01-preview"))
    assert provider.call("router", {"message": "Hello"})["kind"] == "QUESTION"
    assert seen["url"] == "https://open-ai-exam.openai.azure.com/openai/responses"
    assert seen["headers"] == {"api-key": "azure-test"}
    assert seen["params"] == {"api-version": "2025-04-01-preview"}
    assert seen["json"]["model"] == "gpt-5-mini"
    assert seen["json"]["text"] == {"format": {"type": "json_object"}}
    assert seen["json"]["input"][0]["role"] == "system"
    assert seen["json"]["max_output_tokens"] == 16384


def test_provider_retries_persist_separately(store, config):
    class RetriedProvider(FakeProvider):
        last_retries = 1

    _, run_id = queued(store)
    run = execute(store, config, run_id, RetriedProvider([PLAN, CODE, YES]))
    assert run.attempts == 1
    assert run.api_retries == 4


def test_clarification_persists_until_followup(store, config):
    conversation, first = queued(store, content="Build a map")
    clarification = {"kind": "clarification", "summary": "Missing projection", "criteria": [],
                     "clarification_question": "Which coordinate system should I use?"}
    run = execute(store, config, first, FakeProvider([clarification]))
    assert run.status == "needs_clarification"
    with store() as db:
        assert db.get(Conversation, conversation).pending_clarification == clarification["clarification_question"]
    _, second = queued(store, conversation=Conversation(id=conversation, owner_hash=owner_hash("alice")), content="Use WGS84")
    fake = FakeProvider([PLAN, CODE, YES])
    run = execute(store, config, second, fake)
    assert run.status == "approved"
    assert fake.calls[1][1]["pending_clarification"] == clarification["clarification_question"]
    with store() as db:
        assert db.get(Conversation, conversation).pending_clarification is None


def test_recovery_limit_marks_technical_error(store, config):
    config.max_job_recoveries = 0
    _, run_id = queued(store)
    with store() as db:
        _, lease = claim_next(db, config)
        run = db.get(Run, run_id)
        run.heartbeat_at = now() - timedelta(seconds=config.job_stale_seconds + 1)
        db.commit()
    with store() as db:
        assert claim_next(db, config) is None
        run = db.get(Run, run_id)
        assert run.status == "technical_error"
        assert run.lease_id is None


def test_global_concurrency_limit(store, config):
    config.max_concurrent_runs = 1
    _, first = queued(store, "alice")
    _, second = queued(store, "bob")
    with store() as db:
        assert claim_next(db, config)[0] == first
    with store() as db:
        assert claim_next(db, config) is None
        assert db.get(Run, second).status == "queued"


def test_validation_error_is_separate_from_review(store, config):
    class BrokenInfrastructure:
        def validate(self, _files):
            return {"status": "error", "summary": "Sandbox unavailable", "checks": [], "duration_ms": 4}

    _, run_id = queued(store)
    with store() as db:
        _, lease = claim_next(db, config)
    process_run(store, run_id, config, FakeProvider([PLAN, CODE, YES]), lease, BrokenInfrastructure())
    with store() as db:
        run = db.get(Run, run_id)
        assert run.status == "approved"
        assert run.validation_status == "error"


def test_health_and_provisional_pages(store, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "SessionLocal", store)
    client = TestClient(main.app)
    assert client.get("/health").json() == {"status": "ok"}
    assert "Flappy Bird" in client.get("/flappy").text
    page = client.get("/").text
    assert 'sandbox="allow-scripts"' in page
    assert "allow-same-origin" not in page
    assert "connect-src 'none'" in page


def test_production_refuses_sqlite(monkeypatch):
    import app.main as main
    monkeypatch.setattr(main.settings, "app_env", "production")
    with pytest.raises(RuntimeError, match="PostgreSQL"):
        with TestClient(main.app):
            pass
