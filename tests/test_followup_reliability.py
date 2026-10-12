"""Pruebas de aceptación del seguimiento entre turnos.

Se acoplan lo mínimo posible al diseño: comprueban contenido observable (lo que recibe el Coder,
lo que devuelve la API), no nombres de campos nuevos, salvo `client_message_id`, que fija la propuesta.
Reutilizan FakeProvider, PLAN, CODE, YES, NO de tests/test_flow.py.
"""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import Base, Conversation, FileVersion, Message, Run, make_engine
from app.service import owner_hash
from app.snapshots import snapshot_files
from test_flow import CODE, NO, PLAN, YES, FakeProvider, execute


@pytest.fixture
def store(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def config(tmp_path):
    return Settings(auth_tokens="alice,bob", max_coder_attempts=3,
                    langgraph_sqlite_path=str(tmp_path / "cp.sqlite"))


def enqueue(store, conversation_id, content):
    with store() as db:
        if conversation_id is None:
            conversation = Conversation(owner_hash=owner_hash("alice"))
            db.add(conversation)
            db.flush()
            conversation_id = conversation.id
        message = Message(conversation_id=conversation_id, role="user", content=content)
        db.add(message)
        db.flush()
        run = Run(conversation_id=conversation_id, message_id=message.id)
        db.add(run)
        db.commit()
        return conversation_id, run.id


def turn(store, config, conversation_id, content, answers):
    conversation_id, run_id = enqueue(store, conversation_id, content)
    fake = FakeProvider(answers)
    run = execute(store, config, run_id, fake)
    return conversation_id, run, fake


def coder_payload(fake, nth=0):
    return [p for role, p in fake.calls if role == "coder"][nth]


REJECTED = {"summary": "candidato", "files": [{"path": "app.py", "content": "print('CANDIDATO-RECHAZADO')"}]}


# --- A. Base del seguimiento -------------------------------------------------------------
def test_A1_correction_after_exhaustion_receives_rejected_candidate_and_feedback(store, config):
    conv, run, _ = turn(store, config, None, "Build app", [PLAN, REJECTED, NO, REJECTED, NO, REJECTED, NO])
    assert run.status == "exhausted"
    _, _, fake = turn(store, config, conv, "Corrige lo que marco el revisor", [PLAN, CODE, YES])
    seen = json.dumps(coder_payload(fake), ensure_ascii=False)
    assert "CANDIDATO-RECHAZADO" in seen, "el Coder no recibio el candidato rechazado"
    assert "Fix output" in seen, "el Coder no recibio el feedback del Reviewer"


def test_A2_modification_after_approval_still_starts_from_approved_not_rejected(store, config):
    conv, _, _ = turn(store, config, None, "Build app", [PLAN, CODE, YES])
    v2 = {"summary": "v2", "files": [{"path": "app.py", "content": "print('RECHAZADO-V2')"}]}
    conv, run, _ = turn(store, config, conv, "Agrega login", [PLAN, v2, NO, v2, NO, v2, NO])
    assert run.status == "exhausted"
    _, _, fake = turn(store, config, conv, "Agrega logout", [PLAN, CODE, YES])
    seen = json.dumps(coder_payload(fake), ensure_ascii=False)
    assert "print('v1')" in seen and "RECHAZADO-V2" not in seen   # nueva peticion parte de lo aprobado


def test_A3_file_deleted_in_a_later_attempt_does_not_come_back(store, config):
    two = {"summary": "a+b", "files": [{"path": "app.py", "content": "A"}, {"path": "util.py", "content": "B"}]}
    one = {"summary": "a", "files": [{"path": "app.py", "content": "A2"}]}
    conv, run, _ = turn(store, config, None, "Build app", [PLAN, two, NO, one, YES])
    assert run.status == "approved"
    with store() as db:
        head_id = db.get(Conversation, conv).head_snapshot_id
        assert "util.py" not in {f["path"] for f in snapshot_files(db, head_id)}


def test_A4_generated_readme_is_not_part_of_the_coder_base(store, config):
    conv, _, _ = turn(store, config, None, "Build app", [PLAN, CODE, YES])
    _, _, fake = turn(store, config, conv, "Add input", [PLAN, CODE, YES])
    assert "README.md" not in {f["path"] for f in coder_payload(fake)["previous_files"]}


def test_A5_follow_up_base_fits_in_the_coder_budget(store, config):
    big = {"summary": "grande", "files": [{"path": "big.py", "content": "x = 1\n" + "#" * 149_900}]}
    conv, _, _ = turn(store, config, None, "Build app", [PLAN, big, YES])
    _, _, fake = turn(store, config, conv, "Add input", [PLAN, big, YES])
    base = sum(len(f["content"]) for f in coder_payload(fake)["previous_files"])
    assert base <= 150_000


# --- B. Idempotencia ------------------------------------------------------------------------
@pytest.fixture
def client(store, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "SessionLocal", store)
    monkeypatch.setattr(main.settings, "auth_tokens", "alice,bob")
    return TestClient(main.app), {"Authorization": "Bearer alice"}, store


def test_B1_same_client_message_id_returns_same_run(client):
    http, headers, store = client
    cid = http.post("/api/conversations", headers=headers).json()["id"]
    body = {"content": "Build app", "client_message_id": "msg-0001"}
    first = http.post(f"/api/conversations/{cid}/messages", headers=headers, json=body)
    again = http.post(f"/api/conversations/{cid}/messages", headers=headers, json=body)
    assert first.status_code == 202 and again.status_code in (200, 202)
    assert again.json()["run_id"] == first.json()["run_id"]
    with store() as db:
        assert len(db.scalars(select(Run)).all()) == 1
        assert len(db.scalars(select(Message).where(Message.role == "user")).all()) == 1


def test_B2_reusing_the_id_with_different_content_conflicts(client):
    http, headers, _ = client
    cid = http.post("/api/conversations", headers=headers).json()["id"]
    http.post(f"/api/conversations/{cid}/messages", headers=headers,
              json={"content": "Build app", "client_message_id": "msg-0001"})
    other = http.post(f"/api/conversations/{cid}/messages", headers=headers,
                      json={"content": "Otra cosa distinta", "client_message_id": "msg-0001"})
    assert other.status_code == 409


# --- C. Aclaraciones y reintentos -------------------------------------------------------------
def test_C1_pending_clarification_survives_an_unrelated_question(store, config):
    clar = {"kind": "clarification", "summary": "falta", "criteria": [], "clarification_question": "Que CRS?"}
    conv, _, _ = turn(store, config, None, "Build a map", [clar])
    conv, _, _ = turn(store, config, conv, "What does it do?", [{"answer": "Aun nada."}])
    with store() as db:
        assert db.get(Conversation, conv).pending_clarification == "Que CRS?"


def test_C2_retrying_an_older_paused_run_is_refused_once_newer_runs_exist(client, config):
    from app.agents import TemporaryProviderFailure
    http, headers, store = client
    conv, first, _ = turn(store, config, None, "Build A", [TemporaryProviderFailure("Fallo temporal.")])
    assert first.status == "paused"
    turn(store, config, conv, "Build B", [PLAN, CODE, YES])
    assert http.post(f"/api/runs/{first.id}/retry", headers=headers).status_code == 409


# --- D. Caracterizacion de la recuperacion (VERDES hoy; deben seguir verdes tras el cambio) -----
class _Crash(BaseException):
    pass


class _CrashAt(FakeProvider):
    def __init__(self, answers, role):
        super().__init__(answers)
        self.role = role

    def call(self, role, payload):
        if role == self.role:
            raise _Crash()
        return super().call(role, payload)


def _stale_and_reclaim(store, config, run_id):
    from datetime import timedelta
    from app.db import now
    from app.service import claim_next
    with store() as db:
        run = db.get(Run, run_id)
        run.heartbeat_at = now() - timedelta(seconds=config.job_stale_seconds + 1)
        db.commit()
    with store() as db:
        return claim_next(db, config)


def test_D1_recovery_after_crash_mid_graph_resumes_without_duplicating_files(store, config):
    from app.service import claim_next, process_run
    _, run_id = enqueue(store, None, "Build app")
    with store() as db:
        _, lease = claim_next(db, config)
    with pytest.raises(_Crash):
        process_run(store, run_id, config, _CrashAt([PLAN, CODE], "reviewer"), lease)
    claimed = _stale_and_reclaim(store, config, run_id)
    resumed = FakeProvider([YES])   # al reanudar solo corre el Reviewer
    process_run(store, run_id, config, resumed, claimed[1])
    assert [role for role, _ in resumed.calls] == ["reviewer"]           # reanuda, no reinicia
    with store() as db:
        rows = [(f.attempt, f.path) for f in db.scalars(select(FileVersion).where(FileVersion.run_id == run_id)).all()]
        assert sorted(rows) == [(1, "README.md"), (1, "app.py")]         # sin filas duplicadas
        assert db.get(Run, run_id).status == "approved"


def test_D2_recovery_after_graph_finished_but_before_final_commit_does_not_rerun_agents(store, config):
    from app.service import claim_next, process_run
    _, run_id = enqueue(store, None, "Build app")
    with store() as db:
        _, lease = claim_next(db, config)
    process_run(store, run_id, config, FakeProvider([PLAN, CODE, YES]), lease)
    with store() as db:
        for message in db.scalars(select(Message).where(Message.role == "assistant")).all():
            db.delete(message)
        run = db.get(Run, run_id)
        run.status, run.result = "running", None
        db.commit()
    claimed = _stale_and_reclaim(store, config, run_id)
    again = FakeProvider([PLAN, CODE, YES])
    process_run(store, run_id, config, again, claimed[1])
    assert again.calls == []                                             # no vuelve a gastar tokens
    with store() as db:
        assert db.get(Run, run_id).status == "approved"
        assert len(db.scalars(select(Message).where(Message.role == "assistant")).all()) == 1
