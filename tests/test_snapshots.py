import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agents import AgentFailure, build_graph
from app.db import (Conversation, FileBlob, FileVersion, Run, Snapshot, SnapshotFile,
                    ensure_sqlite_compatibility, make_engine)
from app.service import claim_next, owner_hash, process_run
from app.snapshots import snapshot_files, write_snapshot
from app.state import WorkflowState
from test_flow import CODE, NO, PLAN, YES, FakeProvider, config, execute, queued, store


def test_snapshots_are_complete_immutable_and_keep_coder_readme(store, config):
    conversation, run_id = queued(store)
    first = {"summary": "First", "files": [
        {"path": "app.py", "content": "print('old')"},
        {"path": "obsolete.py", "content": "print('remove')"},
    ]}
    final = {"summary": "Final", "files": [
        {"path": "app.py", "content": "print('new')"},
        {"path": "README.md", "content": "User requested readme"},
    ]}
    run = execute(store, config, run_id, FakeProvider([PLAN, first, NO, final, YES]))
    assert (run.status, run.attempts) == ("approved", 2)
    with store() as db:
        snapshots = db.scalars(select(Snapshot).where(Snapshot.run_id == run_id)
                               .order_by(Snapshot.attempt)).all()
        assert [snapshot.attempt for snapshot in snapshots] == [1, 2]
        assert snapshots[0].readme is None
        assert snapshots[1].readme.startswith("# Entrega")
        assert snapshots[1].parent_snapshot_id == snapshots[0].id
        assert snapshots[1].criteria == PLAN["criteria"]
        assert db.get(Conversation, conversation).head_snapshot_id == snapshots[1].id
        assert db.get(Run, run_id).result_snapshot_id == snapshots[1].id
        assert db.get(Run, run_id).finished_at is not None
        for snapshot, expected in zip(snapshots, [first["files"], final["files"]]):
            expected_by_path = {file["path"]: file["content"] for file in expected}
            rows = db.scalars(select(SnapshotFile).where(SnapshotFile.snapshot_id == snapshot.id)).all()
            assert {row.path for row in rows} == set(expected_by_path)
            manifest = []
            for row in sorted(rows, key=lambda item: item.path):
                blob = db.get(FileBlob, row.blob_sha256)
                assert blob.content == expected_by_path[row.path]
                assert blob.size_chars == len(blob.content)
                assert blob.sha256 == hashlib.sha256(blob.content.encode()).hexdigest()
                manifest.append((row.path, row.blob_sha256))
            assert snapshot.manifest_sha256 == hashlib.sha256(
                json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        head_id = db.get(Conversation, conversation).head_snapshot_id
        assert "README.md" in {file["path"] for file in snapshot_files(db, head_id)}
        assert snapshots[1].readme != "User requested readme"
        assert len(db.scalars(select(FileBlob)).all()) == 4
        assert [file.path for file in db.scalars(select(FileVersion).where(
            FileVersion.run_id == run_id, FileVersion.attempt == 2,
            FileVersion.path == "README.md")).all()] == ["README.md", "README.md"]


def test_head_candidate_and_frozen_base_use_snapshot_read(store, config):
    conversation, first_id = queued(store)
    first = execute(store, config, first_id, FakeProvider([PLAN, CODE, YES]))
    with store() as db:
        head_id = db.get(Conversation, conversation).head_snapshot_id
    rejected = {"summary": "Rejected", "files": [
        {"path": "app.py", "content": "print('rejected')"}]}
    _, second_id = queued(store, conversation=Conversation(id=conversation, owner_hash=owner_hash("alice")),
                          content="Change app")
    exhausted = execute(store, config, second_id, FakeProvider([
        PLAN, rejected, NO, rejected, NO, rejected, NO]))
    assert (first.status, exhausted.status) == ("approved", "exhausted")
    with store() as db:
        second = db.get(Run, second_id)
        conversation_row = db.get(Conversation, conversation)
        assert second.base_snapshot_id == head_id
        assert second.base_resolved_at is not None
        assert second.result_snapshot_id == conversation_row.candidate_snapshot_id
        assert conversation_row.head_snapshot_id == head_id
        assert len(db.scalars(select(Snapshot).where(Snapshot.run_id == second_id)).all()) == 3
        assert snapshot_files(db, conversation_row.head_snapshot_id) == CODE["files"]

    _, third_id = queued(store, conversation=Conversation(id=conversation, owner_hash=owner_hash("alice")),
                         content="New request")
    with store() as db:
        claimed, lease = claim_next(db, config)
        assert claimed == third_id
        run = db.get(Run, third_id)
        assert run.base_snapshot_id == head_id
        db.get(Conversation, conversation).head_snapshot_id = exhausted.result_snapshot_id
        db.commit()
    process_run(store, third_id, config, FakeProvider([PLAN, CODE, YES]), lease)
    with store() as db:
        run = db.get(Run, third_id)
        snapshot = db.get(Snapshot, run.result_snapshot_id)
        assert run.base_snapshot_id == head_id
        assert snapshot.parent_snapshot_id == head_id
        assert run.status == "approved"
        assert db.get(Conversation, conversation).candidate_snapshot_id is None


def test_existing_snapshot_cannot_be_rewritten(store, config):
    _, run_id = queued(store)
    execute(store, config, run_id, FakeProvider([PLAN, CODE, YES]))
    with store() as db:
        run = db.get(Run, run_id)
        original = db.get(Snapshot, run.result_snapshot_id)
        version = db.scalar(select(FileVersion).where(
            FileVersion.run_id == run_id, FileVersion.path == "app.py"))
        version.content = "changed after snapshot"
        db.flush()
        assert write_snapshot(db, run, 1, PLAN["criteria"], original.readme).id == original.id
        assert snapshot_files(db, original.id) == CODE["files"]
        with pytest.raises(RuntimeError, match="different content"):
            write_snapshot(db, run, 1, PLAN["criteria"], "changed readme")


@pytest.mark.parametrize("approved", [True, False])
def test_first_turn_final_status_has_snapshot(store, config, approved):
    conversation, run_id = queued(store)
    answers = [PLAN, CODE, YES] if approved else [PLAN, CODE, NO, CODE, NO, CODE, NO]
    run = execute(store, config, run_id, FakeProvider(answers))
    assert run.status == ("approved" if approved else "exhausted")
    with store() as db:
        snapshot = db.get(Snapshot, run.result_snapshot_id)
        assert snapshot.run_id == run_id
        assert snapshot.attempt == run.attempts
        assert snapshot.readme.startswith("# Entrega")
        conversation_row = db.get(Conversation, conversation)
        assert (conversation_row.head_snapshot_id if approved else
                conversation_row.candidate_snapshot_id) == snapshot.id


@pytest.mark.parametrize("approved", [True, False])
def test_first_turn_fails_publicly_if_final_snapshot_cannot_be_written(store, config, monkeypatch, approved):
    import app.service as service

    final_attempt = 1 if approved else config.max_coder_attempts
    original = service.write_snapshot

    def missing_final_snapshot(db, run, attempt, criteria, readme):
        if attempt == final_attempt:
            return None
        return original(db, run, attempt, criteria, readme)

    monkeypatch.setattr(service, "write_snapshot", missing_final_snapshot)
    conversation, run_id = queued(store)
    answers = [PLAN, CODE, YES] if approved else [PLAN, CODE, NO, CODE, NO, CODE, NO]
    run = execute(store, config, run_id, FakeProvider(answers))
    assert run.status == "technical_error"
    assert run.error == "No se pudo guardar la entrega del intento final."
    assert run.result_snapshot_id is None
    with store() as db:
        row = db.get(Conversation, conversation)
        assert row.head_snapshot_id is None
        assert row.candidate_snapshot_id is None


def test_readme_only_coder_output_is_approved_and_snapshotted(store, config):
    conversation, run_id = queued(store)
    readme_only = {"summary": "README only", "files": [
        {"path": "README.md", "content": "User requested readme"}]}
    run = execute(store, config, run_id, FakeProvider([PLAN, readme_only, YES]))
    assert run.status == "approved"
    assert run.error is None
    with store() as db:
        snapshot = db.get(Snapshot, run.result_snapshot_id)
        assert db.get(Conversation, conversation).head_snapshot_id == snapshot.id
        assert snapshot_files(db, snapshot.id) == readme_only["files"]
        assert snapshot.readme.startswith("# Entrega")
    _, next_id = queued(store, conversation=Conversation(id=conversation, owner_hash=owner_hash("alice")),
                        content="Actualiza el README")
    fake = FakeProvider([PLAN, readme_only, YES])
    assert execute(store, config, next_id, fake).status == "approved"
    coder = next(payload for role, payload in fake.calls if role == "coder")
    assert coder["previous_files"] == readme_only["files"]


def test_files_endpoint_keeps_legacy_shape(store, config, monkeypatch):
    import app.main as main

    _, run_id = queued(store)
    execute(store, config, run_id, FakeProvider([PLAN, CODE, YES]))
    monkeypatch.setattr(main, "SessionLocal", store)
    monkeypatch.setattr(main.settings, "auth_tokens", "alice,bob")
    response = TestClient(main.app).get(
        f"/api/runs/{run_id}/files", headers={"Authorization": "Bearer alice"})
    assert response.status_code == 200
    files = response.json()
    assert {tuple(sorted(item)) for item in files} == {("attempt", "content", "path")}
    assert {(item["attempt"], item["path"]) for item in files} == {
        (1, "app.py"), (1, "README.md")}


def test_empty_base_is_frozen_across_reclaim(store, config):
    conversation, run_id = queued(store)
    with store() as db:
        claimed, _ = claim_next(db, config)
        assert claimed == run_id
        run = db.get(Run, run_id)
        frozen_at = run.base_resolved_at
        assert run.base_snapshot_id is None and frozen_at is not None
        run.status = "queued"
        run.lease_id = None
        db.get(Conversation, conversation).head_snapshot_id = "later-head"
        db.commit()
    with store() as db:
        claimed, _ = claim_next(db, config)
        assert claimed == run_id
        run = db.get(Run, run_id)
        assert run.base_snapshot_id is None
        assert run.base_resolved_at == frozen_at


def test_coder_payload_is_rejected_before_model_call(store, config):
    fake = FakeProvider([PLAN])
    graph = build_graph(fake, config, lambda *_: None, validator=object())
    payload = WorkflowState.model_validate({
        "message": "Build app", "history": [], "previous_files": [],
        "scout_references": [{"content": "x" * 450_000}],
    }).model_dump()
    with pytest.raises(AgentFailure, match="demasiado grande"):
        graph.invoke(payload)
    assert [role for role, _ in fake.calls] == ["router", "planner"]


def test_existing_sqlite_database_adds_snapshot_pointer_columns(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE messages (id varchar(36), conversation_id varchar(36), "
                                   "role varchar(16), content text, created_at datetime)")
        connection.exec_driver_sql("CREATE TABLE conversations (id varchar(36), owner_hash varchar(64))")
        connection.exec_driver_sql("CREATE TABLE runs (id varchar(36), conversation_id varchar(36))")
    ensure_sqlite_compatibility(engine)
    ensure_sqlite_compatibility(engine)
    with engine.connect() as connection:
        conversation_columns = {row[1] for row in connection.exec_driver_sql(
            "PRAGMA table_info(conversations)")}
        run_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(runs)")}
    assert {"head_snapshot_id", "candidate_snapshot_id"} <= conversation_columns
    assert {"base_snapshot_id", "base_resolved_at", "result_snapshot_id", "finished_at"} <= run_columns
