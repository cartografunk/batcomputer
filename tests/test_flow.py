from datetime import timedelta
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.agents import AgentFailure, ModelProvider
from app.config import Settings
from app.db import Base, Conversation, FileVersion, Message, Review, Run, Trace, make_engine, now
from app.service import claim_next, latest_files, owner_hash, process_run


class FakeProvider:
    def __init__(self, answers):
        self.answers = iter(answers)
        self.calls = []

    def call(self, role, payload):
        self.calls.append((role, payload))
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
def config():
    return Settings(auth_tokens="alice,bob", max_coder_attempts=3)


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


PLAN = {"kind": "code", "summary": "Create app", "steps": ["Implement"]}
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
        assert len(db.scalars(select(FileVersion)).all()) == 1


def test_rejection_correction_approval(store, config):
    _, run_id = queued(store)
    revised = {"summary": "Fixed", "files": [{"path": "app.py", "content": "print('v2')"}]}
    fake = FakeProvider([PLAN, CODE, NO, revised, YES])
    run = execute(store, config, run_id, fake)
    assert (run.status, run.attempts) == ("approved", 2)
    assert fake.calls[3][1]["review_feedback"] == ["Fix output"]
    with store() as db:
        assert [f.attempt for f in db.scalars(select(FileVersion).order_by(FileVersion.attempt)).all()] == [1, 2]


def test_exhaustion(store, config):
    _, run_id = queued(store)
    run = execute(store, config, run_id, FakeProvider([PLAN, CODE, NO, CODE, NO, CODE, NO]))
    assert (run.status, run.attempts) == ("exhausted", 3)


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
    assert fake.calls[0][1]["files"] == CODE["files"]
    assert fake.calls[1][1]["previous_files"] == CODE["files"]


def test_question_does_not_reimplement(store, config):
    conversation, first = queued(store)
    execute(store, config, first, FakeProvider([PLAN, CODE, YES]))
    _, second = queued(store, conversation=Conversation(id=conversation, owner_hash=owner_hash("alice")), content="What does it do?")
    fake = FakeProvider([{"kind": "question", "summary": "Explain", "answer": "It prints v1.", "steps": []}])
    run = execute(store, config, second, fake)
    assert run.status == "answered"
    assert [role for role, _ in fake.calls] == ["planner"]


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
        assert result["execution"] == "not_executed"
        assert len(client.get(f"/api/runs/{run_id}/traces", headers=headers).json()) == 3
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
    with pytest.raises(AgentFailure, match="agotó los reintentos"):
        provider.call("planner", {"message": "Hello"})
    assert provider.last_retries == 2


def test_provider_retries_persist_separately(store, config):
    class RetriedProvider(FakeProvider):
        last_retries = 1

    _, run_id = queued(store)
    run = execute(store, config, run_id, RetriedProvider([PLAN, CODE, YES]))
    assert run.attempts == 1
    assert run.api_retries == 3
