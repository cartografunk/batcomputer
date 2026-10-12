"""Destructive smoke tests for a disposable local PostgreSQL database only."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
import psycopg
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import Base, Conversation, Message, Run, Snapshot, make_engine, now
from app.service import claim_next, owner_hash
from app.snapshots import _insert_blob, snapshot_files
from test_flow import CODE, NO, PLAN, YES, FakeProvider, execute, queued


pytestmark = pytest.mark.skipif(not os.getenv("TEST_POSTGRES_URL"), reason="TEST_POSTGRES_URL is unset")
MIGRATIONS = (
    "001_initial.sql",
    "002_sandbox_and_recovery.sql",
    "20261007194652_batcomputer_app_role.sql",
    "20261010011458_batcomputer_message_idempotency.sql",
    "20261012000100_batcomputer_snapshots.sql",
)
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def store():
    admin_url = make_url(os.environ["TEST_POSTGRES_URL"])
    if admin_url.get_backend_name() != "postgresql" or admin_url.host not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("TEST_POSTGRES_URL must point to a disposable local PostgreSQL server")
    admin = create_engine(admin_url)
    try:
        with admin.begin() as db:
            db.exec_driver_sql("DROP SCHEMA IF EXISTS batcomputer CASCADE")
            db.exec_driver_sql("CREATE SCHEMA batcomputer")
            for role in ("anon", "authenticated"):
                db.exec_driver_sql(f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') THEN CREATE ROLE {role} NOLOGIN; END IF; END $$")
            db.exec_driver_sql("DROP ROLE IF EXISTS batcomputer_app")
        for name in MIGRATIONS + MIGRATIONS[-2:]:
            with psycopg.connect(admin_url.set(drivername="postgresql").render_as_string(hide_password=False)) as connection:
                connection.execute((ROOT / "migrations" / name).read_text(encoding="utf-8"))
        with admin.begin() as db:
            db.exec_driver_sql("ALTER ROLE batcomputer_app LOGIN PASSWORD 'local'")
        app_url = admin_url.set(username="batcomputer_app", password="local")
        engine = make_engine(app_url.render_as_string(hide_password=False))
        try:
            yield sessionmaker(engine, expire_on_commit=False)
        finally:
            engine.dispose()
    finally:
        admin.dispose()


@pytest.fixture
def config(tmp_path):
    return Settings(auth_tokens="alice,bob", max_coder_attempts=3,
                    langgraph_sqlite_path=str(tmp_path / "postgres-checkpoints.sqlite"))


def test_migrated_schema_has_every_model_column(store):
    inspector = inspect(store.kw["bind"])
    for table in Base.metadata.sorted_tables:
        migrated = {column["name"] for column in inspector.get_columns(table.name, schema="batcomputer")}
        assert {column.name for column in table.columns} <= migrated, table.name


def test_snapshot_tables_allow_select_insert_but_deny_update_delete(store):
    with store() as db:
        assert db.execute(text("SELECT count(*) FROM file_blobs")).scalar_one() >= 0
        assert db.execute(text("SELECT count(*) FROM snapshots")).scalar_one() >= 0
        assert db.execute(text("SELECT count(*) FROM snapshot_files")).scalar_one() >= 0
        conversation = Conversation(owner_hash=owner_hash("alice"))
        db.add(conversation)
        db.flush()
        message = Message(conversation_id=conversation.id, role="user", content="Smoke")
        db.add(message)
        db.flush()
        run = Run(conversation_id=conversation.id, message_id=message.id, status="approved")
        db.add(run)
        db.flush()
        db.execute(text("INSERT INTO file_blobs (sha256, content, size_chars) VALUES (:sha, 'x', 1)"), {"sha": "a" * 64})
        snapshot = Snapshot(conversation_id=conversation.id, run_id=run.id, attempt=1,
                            manifest_sha256="b" * 64, file_count=1, total_chars=1, criteria=[])
        db.add(snapshot)
        db.flush()
        db.execute(text("INSERT INTO snapshot_files (snapshot_id, path, blob_sha256) VALUES (:id, 'x.py', :sha)"),
                   {"id": snapshot.id, "sha": "a" * 64})
        db.commit()
    for table, column in (("file_blobs", "sha256"), ("snapshots", "id"),
                          ("snapshot_files", "snapshot_id")):
        for statement in (f"UPDATE {table} SET {column} = {column} WHERE false",
                          f"DELETE FROM {table} WHERE false"):
            with pytest.raises(DBAPIError) as error:
                with store.kw["bind"].begin() as db:
                    db.exec_driver_sql(statement)
            assert error.value.orig.sqlstate == "42501", statement


def test_blob_conflict_is_allowed_under_rls(store):
    with store() as db:
        first = _insert_blob(db, "same content")
        second = _insert_blob(db, "same content")
        db.commit()
        assert first == second
        assert db.execute(text("SELECT count(*) FROM file_blobs WHERE sha256 = :sha"), {"sha": first}).scalar_one() == 1


def test_approved_exhausted_and_candidate_correction(store, config):
    conversation_id, approved_id = queued(store, content="Build app")
    approved = execute(store, config, approved_id, FakeProvider([PLAN, CODE, YES]))
    assert approved.status == "approved" and approved.result_snapshot_id
    with store() as db:
        head = db.get(Conversation, conversation_id).head_snapshot_id
        assert head == approved.result_snapshot_id
        assert {f["path"] for f in snapshot_files(db, head)} == {"app.py"}

    changed = {"summary": "Rejected", "files": [{"path": "app.py", "content": "print('rejected')"}]}
    _, exhausted_id = queued(store, conversation=Conversation(id=conversation_id), content="Change app")
    exhausted = execute(store, config, exhausted_id,
                        FakeProvider([PLAN, changed, NO, changed, NO, changed, NO]))
    assert exhausted.status == "exhausted" and exhausted.result_snapshot_id
    assert exhausted.base_snapshot_id == head
    with store() as db:
        conversation = db.get(Conversation, conversation_id)
        assert conversation.head_snapshot_id == head
        assert conversation.candidate_snapshot_id == exhausted.result_snapshot_id

    class CandidateProvider(FakeProvider):
        def call(self, role, payload):
            if role == "router":
                self.calls.append((role, payload))
                return {"kind": "MODIFICATION", "reason": "Correct rejected candidate", "base": "candidate"}
            return super().call(role, payload)

    _, correction_id = queued(store, conversation=Conversation(id=conversation_id), content="Fix rejected app")
    provider = CandidateProvider([PLAN, CODE, YES])
    corrected = execute(store, config, correction_id, provider)
    assert corrected.status == "approved" and corrected.result_snapshot_id
    assert corrected.base_snapshot_id == exhausted.result_snapshot_id
    coder = next(payload for role, payload in provider.calls if role == "coder")
    assert coder["previous_files"] == [{"path": "app.py", "content": "print('rejected')"}]
    with store() as db:
        result = db.get(Snapshot, corrected.result_snapshot_id)
        assert result.parent_snapshot_id == exhausted.result_snapshot_id
        conversation = db.get(Conversation, conversation_id)
        assert conversation.head_snapshot_id == corrected.result_snapshot_id
        assert conversation.candidate_snapshot_id is None


def test_claim_next_freezes_base_once(store, config):
    conversation_id, run_id = queued(store, content="Freeze base")
    with store() as db:
        claimed, _ = claim_next(db, config)
        assert claimed == run_id
        original = db.get(Run, run_id)
        assert original.base_snapshot_id is None and original.base_resolved_at is not None
        first_resolution = original.base_resolved_at
        original.heartbeat_at = now() - timedelta(seconds=config.job_stale_seconds + 1)
        db.get(Conversation, conversation_id).head_snapshot_id = "new-head"
        db.commit()
    with store() as db:
        claimed, _ = claim_next(db, config)
        assert claimed == run_id
        recovered = db.get(Run, run_id)
        assert recovered.base_snapshot_id is None
        assert recovered.base_resolved_at == first_resolution


def test_concurrent_idempotency_creates_one_run(store, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "SessionLocal", store)
    monkeypatch.setattr(main.settings, "auth_tokens", "alice,bob")
    headers = {"Authorization": "Bearer alice"}
    conversation_id = TestClient(main.app).post("/api/conversations", headers=headers).json()["id"]
    url = f"/api/conversations/{conversation_id}/messages"
    payload = {"content": "Build app", "client_message_id": "postgres-concurrent"}

    def send(_):
        return TestClient(main.app).post(url, headers=headers, json=payload)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(send, range(2)))
    assert [response.status_code for response in responses] == [202, 202]
    assert responses[0].json()["run_id"] == responses[1].json()["run_id"]
    with store() as db:
        assert db.scalar(select(Run).where(Run.conversation_id == conversation_id)).id == responses[0].json()["run_id"]
        assert len(db.scalars(select(Run).where(Run.conversation_id == conversation_id)).all()) == 1
