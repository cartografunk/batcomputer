"""Immutable per-attempt deliverables written alongside legacy file versions."""

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .db import FileBlob, FileVersion, Run, Snapshot, SnapshotFile


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _insert_blob(db: Session, content: str) -> str:
    digest = _hash(content)
    dialect = db.bind.dialect.name
    if dialect == "postgresql":
        insert = pg_insert(FileBlob)
    elif dialect == "sqlite":
        insert = sqlite_insert(FileBlob)
    else:
        raise RuntimeError(f"Unsupported snapshot database: {dialect}")
    db.execute(insert.values(sha256=digest, content=content, size_chars=len(content))
               .on_conflict_do_nothing(index_elements=[FileBlob.sha256]))
    return digest


def snapshot_files(db: Session, snapshot_id: str | None) -> list[dict]:
    if snapshot_id is None:
        return []
    rows = db.execute(select(SnapshotFile.path, FileBlob.content).join(
        FileBlob, FileBlob.sha256 == SnapshotFile.blob_sha256).where(
        SnapshotFile.snapshot_id == snapshot_id).order_by(SnapshotFile.path)).all()
    return [{"path": path, "content": content} for path, content in rows]


def write_snapshot(db: Session, run: Run, attempt: int, criteria: list[dict], readme: str | None):
    """Insert once for a reviewed attempt; the README is known for the final one."""
    existing = db.scalar(select(Snapshot).where(Snapshot.run_id == run.id, Snapshot.attempt == attempt))
    if existing is not None:
        if existing.readme != readme:
            raise RuntimeError("A snapshot was already written with different content")
        return existing
    rows = db.scalars(select(FileVersion).where(
        FileVersion.run_id == run.id, FileVersion.attempt == attempt,
    ).order_by(FileVersion.created_at, FileVersion.id)).all()
    files = {row.path: row.content for row in rows}
    if not files:
        return None
    entries = [(path, _hash(content)) for path, content in sorted(files.items())]
    manifest = _hash(json.dumps(entries, ensure_ascii=False, separators=(",", ":")))
    previous = (db.scalar(select(Snapshot).where(
        Snapshot.run_id == run.id, Snapshot.attempt == attempt - 1)) if attempt > 1 else None)
    parent_id = previous.id if previous else run.base_snapshot_id
    parent = db.get(Snapshot, parent_id) if parent_id else None
    accumulated = list(parent.criteria) if parent else []
    for criterion in criteria:
        if criterion not in accumulated:
            accumulated.append(criterion)
    snapshot = Snapshot(
        conversation_id=run.conversation_id, run_id=run.id, attempt=attempt,
        parent_snapshot_id=parent_id, manifest_sha256=manifest,
        file_count=len(entries), total_chars=sum(map(len, files.values())),
        criteria=accumulated, readme=readme,
    )
    db.add(snapshot)
    db.flush()
    for path, content in sorted(files.items()):
        digest = _insert_blob(db, content)
        db.add(SnapshotFile(snapshot_id=snapshot.id, path=path, blob_sha256=digest))
    return snapshot
