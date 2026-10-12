from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import settings


def now():
    return datetime.now(timezone.utc)


def uid():
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    owner_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    pending_clarification: Mapped[str | None] = mapped_column(Text, nullable=True)
    head_snapshot_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    candidate_snapshot_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    client_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (Index(
        "ux_messages_idempotency", "conversation_id", "client_message_id", unique=True,
        postgresql_where=client_message_id.is_not(None),
        sqlite_where=client_message_id.is_not(None),
    ),)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    message_id: Mapped[str] = mapped_column(ForeignKey("messages.id"))
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    api_retries: Mapped[int] = mapped_column(Integer, default=0)
    recovery_count: Mapped[int] = mapped_column(Integer, default=0)
    validation_status: Mapped[str] = mapped_column(String(24), default="not_executed")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    base_snapshot_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    base_resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result_snapshot_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Plan(Base):
    __tablename__ = "plans"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    content: Mapped[dict] = mapped_column(JSON)


class Review(Base):
    __tablename__ = "reviews"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    attempt: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict] = mapped_column(JSON)


class Validation(Base):
    __tablename__ = "validations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    attempt: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24))
    content: Mapped[dict] = mapped_column(JSON)


class FileVersion(Base):
    __tablename__ = "file_versions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    attempt: Mapped[int] = mapped_column(Integer)
    path: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class FileBlob(Base):
    __tablename__ = "file_blobs"
    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    content: Mapped[str] = mapped_column(Text)
    size_chars: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Snapshot(Base):
    __tablename__ = "snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    attempt: Mapped[int] = mapped_column(Integer)
    parent_snapshot_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    file_count: Mapped[int] = mapped_column(Integer)
    total_chars: Mapped[int] = mapped_column(Integer)
    criteria: Mapped[list] = mapped_column(JSON, default=list)
    readme: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    __table_args__ = (Index("ux_snapshots_run_attempt", "run_id", "attempt", unique=True),)


class SnapshotFile(Base):
    __tablename__ = "snapshot_files"
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("snapshots.id"), primary_key=True)
    path: Mapped[str] = mapped_column(Text, primary_key=True)
    blob_sha256: Mapped[str] = mapped_column(ForeignKey("file_blobs.sha256"))


class Trace(Base):
    __tablename__ = "traces"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    stage: Mapped[str] = mapped_column(String(32))
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    input: Mapped[dict] = mapped_column(JSON)
    output: Mapped[dict] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


def make_engine(url: str):
    kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _fk(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
    elif url.startswith("postgresql"):
        @event.listens_for(engine, "connect")
        def _schema(connection, _):
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION search_path TO batcomputer")
            connection.commit()
    return engine


engine = make_engine(settings.database_url)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def ensure_sqlite_compatibility(sqlite_engine):
    """Add nullable columns to existing local development databases."""
    if sqlite_engine.dialect.name != "sqlite":
        return
    with sqlite_engine.begin() as connection:
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(messages)")}
        if "client_message_id" not in columns:
            connection.exec_driver_sql("ALTER TABLE messages ADD COLUMN client_message_id varchar(64)")
        if "request_hash" not in columns:
            connection.exec_driver_sql("ALTER TABLE messages ADD COLUMN request_hash varchar(64)")
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_messages_idempotency "
            "ON messages (conversation_id, client_message_id) WHERE client_message_id IS NOT NULL"
        )
        for table, additions in {
            "conversations": {
                "head_snapshot_id": "varchar(36)",
                "candidate_snapshot_id": "varchar(36)",
            },
            "runs": {
                "base_snapshot_id": "varchar(36)",
                "base_resolved_at": "datetime",
                "result_snapshot_id": "varchar(36)",
                "finished_at": "datetime",
            },
        }.items():
            existing = {row[1] for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")}
            if not existing:
                continue
            for column, column_type in additions.items():
                if column not in existing:
                    connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
