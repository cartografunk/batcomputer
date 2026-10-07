import hashlib
import json
from datetime import timedelta

from sqlalchemy import and_, exists, func, select, update
from sqlalchemy.orm import Session

from .agents import AgentFailure, ModelProvider, build_graph
from .config import Settings
from .db import Conversation, FileVersion, Message, Plan, Review, Run, Trace, now, uid


def owner_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def owned_conversation(db: Session, conversation_id: str, owner: str):
    return db.scalar(select(Conversation).where(Conversation.id == conversation_id, Conversation.owner_hash == owner))


def owned_run(db: Session, run_id: str, owner: str):
    return db.scalar(select(Run).join(Conversation).where(Run.id == run_id, Conversation.owner_hash == owner))


def latest_files(db: Session, conversation_id: str, before_run: Run | None = None):
    query = select(FileVersion, Run).join(Run).where(FileVersion.conversation_id == conversation_id, Run.status == "approved")
    if before_run:
        query = query.where(Run.created_at < before_run.created_at)
    versions = db.execute(query.order_by(FileVersion.created_at, FileVersion.id)).all()
    files = {}
    for version, _ in versions:
        files[version.path] = {"path": version.path, "content": version.content}
    return list(files.values())


def claim_next(db: Session, settings: Settings):
    stale = now() - timedelta(seconds=settings.job_stale_seconds)
    db.execute(update(Run).where(Run.status == "running", Run.heartbeat_at < stale).values(status="queued", updated_at=now()))
    db.commit()
    queued = db.scalars(select(Run).where(Run.status == "queued").order_by(Run.created_at, Run.id).limit(50)).all()
    for candidate in queued:
        if db.bind.dialect.name == "postgresql":
            # Serializes claims for one conversation across Railway replicas.
            locked = db.scalar(select(func.pg_try_advisory_xact_lock(func.hashtext(candidate.conversation_id))))
            if not locked:
                db.rollback()
                continue
        running = db.scalar(select(Run.id).where(Run.conversation_id == candidate.conversation_id, Run.status == "running"))
        earlier = db.scalar(select(Run.id).where(
            Run.conversation_id == candidate.conversation_id, Run.status == "queued",
            Run.created_at < candidate.created_at,
        ))
        if running or earlier:
            db.rollback()
            continue
        lease_id = uid()
        changed = db.execute(update(Run).where(Run.id == candidate.id, Run.status == "queued").values(
            status="running", heartbeat_at=now(), updated_at=now(), lease_id=lease_id
        ))
        if changed.rowcount:
            db.commit()
            return candidate.id, lease_id
        db.rollback()
    return None


class LeaseLost(Exception):
    pass


def process_run(session_factory, run_id: str, settings: Settings, provider=None, lease_id=None):
    provider = provider or ModelProvider(settings)

    def check_lease(run):
        if lease_id is not None and (run.lease_id != lease_id or run.status != "running"):
            raise LeaseLost()

    def record(role, attempt, payload, output, error, duration):
        with session_factory() as db:
            run = db.get(Run, run_id)
            check_lease(run)
            run.heartbeat_at = now()
            run.updated_at = now()
            run.api_retries += getattr(provider, "last_retries", 0)
            db.add(Trace(run_id=run_id, stage=role, attempt=attempt, duration_ms=duration,
                         input=payload, output=output, error=error))
            if not error:
                if role == "planner":
                    db.add(Plan(run_id=run_id, content=output))
                elif role == "reviewer":
                    db.add(Review(run_id=run_id, attempt=attempt, content=output))
                elif role == "coder":
                    run.attempts = attempt
                    for file in output["files"]:
                        db.add(FileVersion(conversation_id=run.conversation_id, run_id=run_id,
                                           attempt=attempt, path=file["path"], content=file["content"]))
            db.commit()

    try:
        with session_factory() as db:
            run = db.get(Run, run_id)
            check_lease(run)
            message = db.get(Message, run.message_id)
            history = db.scalars(select(Message).where(Message.conversation_id == run.conversation_id,
                                                       Message.created_at < message.created_at).order_by(Message.created_at)).all()
            previous = latest_files(db, run.conversation_id, run)
            payload = {"message": message.content,
                       "history": [{"role": m.role, "content": m.content} for m in history[-20:]],
                       "previous_files": previous}
        graph = build_graph(provider, settings, record)
        result = graph.invoke(payload)
        with session_factory() as db:
            run = db.get(Run, run_id)
            check_lease(run)
            run.status = result["status"]
            run.result = result["result"]
            run.attempts = result.get("attempts", 0)
            run.updated_at = now()
            db.add(Message(conversation_id=run.conversation_id, role="assistant", content=run.result))
            db.commit()
    except LeaseLost:
        return
    except Exception as exc:
        # Only public error text is persisted; provider exceptions may contain request headers.
        public = str(exc) if isinstance(exc, AgentFailure) else "Error técnico durante la ejecución. Inténtalo de nuevo."
        with session_factory() as db:
            run = db.get(Run, run_id)
            check_lease(run)
            run.status = "technical_error"
            run.error = public
            run.result = public
            run.updated_at = now()
            db.add(Message(conversation_id=run.conversation_id, role="assistant", content=public))
            db.commit()


def public_run(run: Run):
    return {"id": run.id, "conversation_id": run.conversation_id, "status": run.status,
            "result": run.result, "attempts": run.attempts, "api_retries": run.api_retries,
            "execution": "not_executed",
            "error": run.error, "created_at": run.created_at, "updated_at": run.updated_at}
