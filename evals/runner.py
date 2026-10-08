"""Ejecuta un escenario dentro del proceso y recoge lo que decidió cada agente.

Cada escenario usa una base SQLite y un checkpointer temporales, el mismo worker
(claim_next + process_run) que producción y el proveedor configurado en .env. No toca la
base de datos real ni necesita el servidor web.
"""

import shutil
import tempfile
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import Base, Conversation, FileVersion, Message, Run, Trace, make_engine
from app.lc_provider import ROLES, make_provider, resolve_spec
from app.service import claim_next, owner_hash, process_run

from .dryrun import INVALID_KEY
from .scenarios import Scenario
from .seeds import SEEDS

MAX_FILE_CHARS = 20_000
MAX_TOTAL_CHARS = 120_000
FAILURE_OVERRIDES = {
    "auth": {"model_api_key": INVALID_KEY, "openai_api_key": INVALID_KEY, "anthropic_api_key": INVALID_KEY,
             "openrouter_api_key": INVALID_KEY, "gemini_api_key": INVALID_KEY, "model_api_retries": 0},
    "timeout": {"model_timeout_seconds": 0.001, "model_api_retries": 0},
}


def scenario_settings(base: Settings, scenario: Scenario, workdir: Path) -> Settings:
    update = {"database_url": f"sqlite:///{(workdir / 'eval.db').as_posix()}",
              "langgraph_sqlite_path": str(workdir / "checkpoints.sqlite"),
              "auth_tokens": "eval"}
    update.update(FAILURE_OVERRIDES.get(scenario.failure or "", {}))
    return base.model_copy(update=update)


def describe_models(settings: Settings, provider=None) -> dict:
    """Modelo por rol sin claves. El backend http usa la misma selección que default_spec."""
    describe = getattr(provider, "describe", None)
    if callable(describe):
        return describe()
    described = {}
    for role in ROLES:
        try:
            described[role] = resolve_spec(settings, role).label
        except ValueError as exc:
            described[role] = f"error: {exc}"
    return described


def _usage(provider) -> dict:
    return {label: dict(entry) for label, entry in (getattr(provider, "usage", None) or {}).items()}


def _usage_delta(before: dict, after: dict) -> dict:
    delta = {}
    for label, entry in after.items():
        previous = before.get(label, {})
        diff = {key: value - previous.get(key, 0) for key, value in entry.items()}
        if any(diff.values()):
            delta[label] = diff
    return delta


def _cap_files(files: list[dict]) -> list[dict]:
    capped, total = [], 0
    for item in files:
        content = item["content"]
        room = max(0, min(MAX_FILE_CHARS, MAX_TOTAL_CHARS - total))
        truncated = len(content) > room
        capped.append({"path": item["path"], "content": content[:room], "truncated": truncated,
                       "chars": len(content)})
        total += min(len(content), room)
    return capped


def _enqueue(store, conversation_id: str, content: str) -> str:
    with store() as db:
        message = Message(conversation_id=conversation_id, role="user", content=content)
        db.add(message)
        db.flush()
        run = Run(conversation_id=conversation_id, message_id=message.id)
        db.add(run)
        db.commit()
        return run.id


def collect_turn(store, run_id: str, message: str, elapsed: float, usage: dict) -> dict:
    with store() as db:
        run = db.get(Run, run_id)
        traces = db.scalars(select(Trace).where(Trace.run_id == run_id).order_by(Trace.created_at, Trace.id)).all()
        versions = db.scalars(select(FileVersion).where(FileVersion.run_id == run_id)).all()
        final_attempt = max((version.attempt for version in versions), default=None)
        files = [{"path": v.path, "content": v.content} for v in versions if v.attempt == final_attempt]
        return {
            "message": message,
            "run_id": run_id,
            "status": run.status,
            "result": run.result or "",
            "error": run.error,
            "attempts": run.attempts,
            "api_retries": run.api_retries,
            "validation_status": run.validation_status,
            "route": next((t.output.get("kind") for t in traces if t.stage == "router" and not t.error), None),
            "plan": next((t.output for t in reversed(traces) if t.stage == "planner" and not t.error), None),
            "coder_inputs": [{"attempt": t.attempt, "previous_files": len(t.input.get("previous_files") or []),
                              "review_feedback": len(t.input.get("review_feedback") or [])}
                             for t in traces if t.stage == "coder"],
            "reviews": [{"attempt": t.attempt, "approved": t.output.get("approved"), "summary": t.output.get("summary"),
                         "feedback": t.output.get("feedback", [])}
                        for t in traces if t.stage == "reviewer" and not t.error],
            "validations": [{"attempt": t.attempt, "status": t.output.get("status"), "summary": t.output.get("summary")}
                            for t in traces if t.stage == "validator"],
            "errors": [{"stage": t.stage, "attempt": t.attempt, "error": t.error} for t in traces if t.error],
            "files": _cap_files(files),
            "duration_s": round(elapsed, 2),
            "usage": usage,
        }


def run_scenario(scenario: Scenario, base_settings: Settings, provider_factory=make_provider,
                 keep_workdir: bool = False) -> dict:
    workdir = Path(tempfile.mkdtemp(prefix=f"batcomputer-eval-{scenario.id}-"))
    settings = scenario_settings(base_settings, scenario, workdir)
    engine = make_engine(settings.database_url)
    try:
        Base.metadata.create_all(engine)
        store = sessionmaker(engine, expire_on_commit=False)
        with store() as db:
            conversation = Conversation(owner_hash=owner_hash("eval"))
            db.add(conversation)
            db.commit()
            conversation_id = conversation.id
        if scenario.seed:
            SEEDS[scenario.seed](store, conversation_id)
        provider = provider_factory(settings)
        turns = []
        for message in scenario.turns:
            run_id = _enqueue(store, conversation_id, message)
            with store() as db:
                claimed = claim_next(db, settings)
            if not claimed or claimed[0] != run_id:
                raise RuntimeError(f"El worker no reclamó el run del escenario {scenario.id}.")
            before, started = _usage(provider), time.monotonic()
            process_run(store, run_id, settings, provider, claimed[1])
            turns.append(collect_turn(store, run_id, message, time.monotonic() - started,
                                      _usage_delta(before, _usage(provider))))
        return {"scenario_id": scenario.id, "backend": settings.model_backend,
                "models": describe_models(settings, provider), "turns": turns, "final": turns[-1]}
    finally:
        engine.dispose()
        if not keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)
