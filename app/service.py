import hashlib
import threading
from datetime import timedelta
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from .agents import AgentFailure, TemporaryProviderFailure, build_graph
from .config import Settings
from .db import (Conversation, FileVersion, Message, Plan, Review, Run,
                 Snapshot, Trace, Validation, now, uid)
from .lc_provider import make_provider
from .observability import configure_tracing, graph_config
from .sandbox import E2BValidator
from .snapshots import snapshot_files, write_snapshot
from .state import WorkflowState


def owner_hash(token: str) -> str:
    digest = hashlib.sha256(token.encode()).hexdigest()
    if token.startswith("guest:"):
        return "g" + digest[:63]
    if token.startswith("user:"):
        return "u" + digest[:63]
    return digest


def owned_conversation(db: Session, conversation_id: str, owner: str):
    return db.scalar(select(Conversation).where(Conversation.id == conversation_id, Conversation.owner_hash == owner))


def owned_run(db: Session, run_id: str, owner: str):
    return db.scalar(select(Run).join(Conversation).where(Run.id == run_id, Conversation.owner_hash == owner))


def followup_context(db: Session, run: Run):
    """Freeze the last rejected candidate and prior criteria in graph input."""
    last_run = db.scalar(select(Run).where(
        Run.conversation_id == run.conversation_id,
        Run.created_at < run.created_at,
    ).order_by(Run.created_at.desc(), Run.id.desc()).limit(1))
    previous = db.scalars(select(Run).where(
        Run.conversation_id == run.conversation_id,
        Run.created_at < run.created_at,
        Run.status.in_(["approved", "exhausted"]),
    ).order_by(Run.created_at.desc(), Run.id.desc()).limit(2)).all()
    candidate = previous[0] if previous and previous[0].status == "exhausted" else None
    approved = next((item for item in previous if item.status == "approved"), None)
    if approved is None:
        approved = db.scalar(select(Run).where(
            Run.conversation_id == run.conversation_id,
            Run.created_at < run.created_at, Run.status == "approved",
        ).order_by(Run.created_at.desc(), Run.id.desc()).limit(1))

    def criteria_for(prior_run):
        if prior_run is None or prior_run.result_snapshot_id is None:
            return []
        snapshot = db.get(Snapshot, prior_run.result_snapshot_id)
        return snapshot.criteria if snapshot else []

    files = []
    feedback = []
    summary = ""
    snapshot_id = None
    if candidate:
        snapshot_id = candidate.result_snapshot_id
        files = snapshot_files(db, snapshot_id)
        review = db.scalar(select(Review).where(
            Review.run_id == candidate.id, Review.attempt == candidate.attempts).limit(1))
        if review:
            feedback = [str(item)[:1000] for item in review.content.get("feedback", [])[:20]]
            summary = str(review.content.get("summary", ""))[:500]
    return {
        "candidate_files": files,
        "candidate_feedback": feedback,
        "candidate_review_summary": summary,
        "candidate_snapshot_id": snapshot_id,
        "candidate_criteria": criteria_for(candidate),
        "approved_criteria": criteria_for(approved),
        "last_run_status": last_run.status if last_run else None,
    }


def turn_ledger(db: Session, run: Run):
    prior = db.scalars(select(Run).where(
        Run.conversation_id == run.conversation_id,
        Run.created_at < run.created_at,
    ).order_by(Run.created_at.desc(), Run.id.desc()).limit(6)).all()
    entries = []
    for number, item in enumerate(reversed(prior), 1):
        message = db.get(Message, item.message_id)
        first_line = message.content.splitlines()[0][:200] if message else ""
        entries.append({"number": number, "request": first_line, "status": item.status,
                        "attempts": item.attempts, "snapshot_id": item.result_snapshot_id})
    return entries


def claim_next(db: Session, settings: Settings):
    if db.bind.dialect.name == "postgresql":
        # One short transaction coordinates claims and the global concurrency limit.
        if not db.scalar(select(func.pg_try_advisory_xact_lock(4532751))):
            db.rollback()
            return None
    stale = now() - timedelta(seconds=settings.job_stale_seconds)
    for interrupted in db.scalars(select(Run).where(Run.status == "running", Run.heartbeat_at < stale)).all():
        interrupted.lease_id = None
        interrupted.updated_at = now()
        if interrupted.recovery_count >= settings.max_job_recoveries:
            interrupted.status = "technical_error"
            interrupted.error = "El trabajo se interrumpió repetidamente; vuelve a enviar el mensaje."
            interrupted.result = interrupted.error
            interrupted.finished_at = now()
            db.add(Message(conversation_id=interrupted.conversation_id, role="assistant", content=interrupted.result))
        else:
            interrupted.status = "queued"
            interrupted.recovery_count += 1
    db.flush()
    running_count = db.scalar(select(func.count(Run.id)).where(Run.status == "running"))
    if running_count >= settings.max_concurrent_runs:
        db.commit()
        return None
    queued = db.scalars(select(Run).where(Run.status == "queued").order_by(Run.created_at, Run.id).limit(50)).all()
    for candidate in queued:
        running = db.scalar(select(Run.id).where(Run.conversation_id == candidate.conversation_id, Run.status == "running"))
        earlier = db.scalar(select(Run.id).where(
            Run.conversation_id == candidate.conversation_id, Run.status == "queued",
            or_(Run.created_at < candidate.created_at,
                and_(Run.created_at == candidate.created_at, Run.id < candidate.id)),
        ))
        if running or earlier:
            continue
        lease_id = uid()
        values = dict(status="running", heartbeat_at=now(), updated_at=now(), lease_id=lease_id)
        if candidate.base_resolved_at is None:
            conversation = db.get(Conversation, candidate.conversation_id)
            values.update(base_snapshot_id=conversation.head_snapshot_id, base_resolved_at=now())
        changed = db.execute(update(Run).where(Run.id == candidate.id, Run.status == "queued").values(**values))
        if changed.rowcount:
            db.commit()
            return candidate.id, lease_id
    db.commit()
    return None


class LeaseLost(Exception):
    pass


def process_run(session_factory, run_id: str, settings: Settings, provider=None, lease_id=None, validator=None,
                scout=None):
    provider = provider or make_provider(settings)
    validator = validator or E2BValidator(settings)
    heartbeat_stop = threading.Event()

    def heartbeat():
        interval = max(0.2, min(settings.job_stale_seconds / 3, 20))
        while not heartbeat_stop.wait(interval):
            try:
                with session_factory() as db:
                    changed = db.execute(update(Run).where(
                        Run.id == run_id, Run.status == "running", Run.lease_id == lease_id
                    ).values(heartbeat_at=now()))
                    db.commit()
                    if not changed.rowcount:
                        return
            except Exception:
                # The worker's stale-job recovery handles a persistent outage.
                pass

    heartbeat_thread = None
    if lease_id is not None:
        heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
        heartbeat_thread.start()

    def check_lease(run):
        if lease_id is not None and (run.lease_id != lease_id or run.status != "running"):
            raise LeaseLost()

    def record(role, attempt, payload, output, error, duration):
        with session_factory() as db:
            run = db.get(Run, run_id)
            check_lease(run)
            run.heartbeat_at = now()
            run.updated_at = now()
            if role in ("router", "question", "planner", "coder", "reviewer"):
                run.api_retries += getattr(provider, "last_retries", 0)
            if role == "coder":
                run.attempts = max(run.attempts, attempt)
            if role == "router" and output.get("base") == "candidate" and payload.get("candidate_snapshot_id"):
                run.base_snapshot_id = payload["candidate_snapshot_id"]
            db.add(Trace(run_id=run_id, stage=role, attempt=attempt, duration_ms=duration,
                         input=payload, output=output, error=error))
            if not error:
                if role == "planner":
                    db.add(Plan(run_id=run_id, content=output))
                elif role == "reviewer":
                    db.add(Review(run_id=run_id, attempt=attempt, content=output))
                    if not output["approved"] and attempt < settings.max_coder_attempts:
                        if write_snapshot(db, run, attempt, payload["plan"]["criteria"], None) is None:
                            raise AgentFailure("No se pudo guardar la entrega del intento revisado.")
                elif role == "validator":
                    run.validation_status = output["status"]
                    db.add(Validation(run_id=run_id, attempt=attempt, status=output["status"], content=output))
                elif role == "coder":
                    for file in output["files"]:
                        db.add(FileVersion(conversation_id=run.conversation_id, run_id=run_id,
                                           attempt=attempt, path=file["path"], content=file["content"]))
                elif role == "documenter":
                    existing = db.scalar(select(Snapshot).where(
                        Snapshot.run_id == run_id, Snapshot.attempt == attempt))
                    if existing is None:
                        if write_snapshot(db, run, attempt, payload["criteria"], output["readme"]) is None:
                            raise AgentFailure("No se pudo guardar la entrega del intento final.")
                        db.add(FileVersion(conversation_id=run.conversation_id, run_id=run_id,
                                           attempt=attempt, path="README.md", content=output["readme"]))
                    elif existing.readme != output["readme"]:
                        raise AgentFailure("El resultado del intento cambió tras guardarse.")
            db.commit()

    try:
        with session_factory() as db:
            run = db.get(Run, run_id)
            check_lease(run)
            message = db.get(Message, run.message_id)
            history = db.scalars(select(Message).where(Message.conversation_id == run.conversation_id,
                                                       Message.created_at < message.created_at).order_by(Message.created_at)).all()
            previous = snapshot_files(db, run.base_snapshot_id)
            followup = followup_context(db, run)
            ledger = turn_ledger(db, run)
            pending = db.get(Conversation, run.conversation_id).pending_clarification
            payload = {"message": message.content,
                       "history": [{"role": m.role, "content": m.content[:4000]} for m in history[-4:]],
                       "turn_ledger": ledger,
                       "previous_files": previous, "pending_clarification": pending,
                       **followup}
            conversation_id = run.conversation_id
        checkpoint_path = Path(settings.langgraph_sqlite_path)
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        configure_tracing(settings)
        with SqliteSaver.from_conn_string(str(checkpoint_path)) as checkpointer:
            graph = build_graph(provider, settings, record, validator, checkpointer=checkpointer, scout=scout)
            result = graph.invoke(WorkflowState.model_validate(payload).model_dump(),
                                  graph_config(settings, run_id, conversation_id, provider))
        with session_factory() as db:
            run = db.get(Run, run_id)
            check_lease(run)
            run.status = result["status"]
            run.result = result["result"]
            run.attempts = result.get("attempts", 0)
            run.updated_at = now()
            run.finished_at = now()
            conversation = db.get(Conversation, run.conversation_id)
            if result["status"] in ("approved", "exhausted"):
                snapshot = db.scalar(select(Snapshot).where(
                    Snapshot.run_id == run.id, Snapshot.attempt == run.attempts))
                if snapshot is None:
                    raise AgentFailure("No se pudo guardar la entrega del intento final.")
                run.result_snapshot_id = snapshot.id
                if result["status"] == "approved":
                    conversation.head_snapshot_id = snapshot.id
                    conversation.candidate_snapshot_id = None
                else:
                    conversation.candidate_snapshot_id = snapshot.id
            if result["status"] == "needs_clarification":
                conversation.pending_clarification = result["result"]
            elif result["status"] in ("approved", "exhausted"):
                conversation.pending_clarification = None
            db.add(Message(conversation_id=run.conversation_id, role="assistant", content=run.result))
            db.commit()
    except LeaseLost:
        return
    except TemporaryProviderFailure as exc:
        with session_factory() as db:
            run = db.get(Run, run_id)
            check_lease(run)
            run.status = "paused"
            run.error = str(exc)
            run.result = str(exc)
            run.lease_id = None
            run.updated_at = now()
            db.add(Message(conversation_id=run.conversation_id, role="assistant", content=run.result))
            db.commit()
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
            run.finished_at = now()
            db.add(Message(conversation_id=run.conversation_id, role="assistant", content=public))
            db.commit()
    finally:
        heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=1)


def public_run(run: Run):
    return {"id": run.id, "conversation_id": run.conversation_id, "status": run.status,
            "result": run.result, "attempts": run.attempts, "api_retries": run.api_retries,
            "validation_status": run.validation_status, "recovery_count": run.recovery_count,
            "error": run.error, "created_at": run.created_at, "updated_at": run.updated_at}
