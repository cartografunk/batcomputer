import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .config import settings
from .db import Base, Conversation, FileVersion, Message, Run, SessionLocal, Trace, engine, now
from .service import claim_next, owned_conversation, owned_run, owner_hash, process_run, public_run

logger = logging.getLogger(__name__)


async def worker_loop():
    active = None
    try:
        while True:
            try:
                with SessionLocal() as db:
                    claimed = claim_next(db, settings)
                if claimed:
                    run_id, lease_id = claimed
                    active = asyncio.create_task(asyncio.to_thread(process_run, SessionLocal, run_id, settings, lease_id=lease_id))
                    await asyncio.shield(active)
                    active = None
                else:
                    await asyncio.sleep(settings.worker_poll_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # No exception text: SDK/DB errors can contain credentials or URLs.
                logger.error("worker failure: %s", type(exc).__name__)
                await asyncio.sleep(settings.worker_poll_seconds)
    except asyncio.CancelledError:
        if active is not None:
            try:
                await asyncio.wait_for(asyncio.shield(active), timeout=settings.shutdown_grace_seconds)
            except (asyncio.TimeoutError, Exception):
                pass
        raise


@asynccontextmanager
async def lifespan(_app):
    if settings.app_env == "production":
        if not settings.database_url.startswith("postgresql+psycopg://"):
            raise RuntimeError("Production requires PostgreSQL DATABASE_URL")
        if not settings.auth_tokens.strip() or (settings.model_mode != "ollama" and not settings.model_api_key.strip()):
            raise RuntimeError("Production requires AUTH_TOKENS and MODEL_API_KEY")
        if not Path(settings.langgraph_sqlite_path).is_absolute():
            raise RuntimeError("Production requires a persistent absolute LANGGRAPH_SQLITE_PATH")
        if not (Path(settings.frontend_dir) / "index.html").is_file():
            raise RuntimeError("Production requires a frontend index.html")
    if settings.database_url.startswith("sqlite"):
        Base.metadata.create_all(engine)
    task = asyncio.create_task(worker_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Batcomputer", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[x.strip() for x in settings.cors_origins.split(",") if x.strip()],
                   allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"])
frontend = Path(settings.frontend_dir).resolve()
if (frontend / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=frontend / "assets"), name="frontend-assets")


def db_session():
    with SessionLocal() as db:
        yield db


def auth(authorization: str = Header(default="")):
    token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
    valid = any(secrets.compare_digest(token, configured.strip()) for configured in settings.auth_tokens.split(",") if configured.strip())
    if not valid:
        raise HTTPException(401, "Token de acceso inválido")
    return owner_hash(token)


class MessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=20000)


@app.get("/")
def home():
    return FileResponse(frontend / "index.html")


@app.get("/flappy")
def flappy():
    return FileResponse(Path(__file__).parent / "static" / "flappy.html")


@app.get("/health")
def health(db: Session = Depends(db_session)):
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception:
        return JSONResponse({"status": "unavailable"}, status_code=503)


@app.post("/api/conversations", status_code=201)
def create_conversation(owner: str = Depends(auth), db: Session = Depends(db_session)):
    conversation = Conversation(owner_hash=owner)
    db.add(conversation)
    db.commit()
    return {"id": conversation.id, "created_at": conversation.created_at}


@app.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: str, owner: str = Depends(auth), db: Session = Depends(db_session)):
    conversation = owned_conversation(db, conversation_id, owner)
    if not conversation:
        raise HTTPException(404, "Conversación no encontrada")
    messages = db.scalars(select(Message).where(Message.conversation_id == conversation_id).order_by(Message.created_at, Message.id)).all()
    return {"id": conversation.id, "created_at": conversation.created_at,
            "pending_clarification": conversation.pending_clarification,
            "messages": [{"id": m.id, "role": m.role, "content": m.content, "created_at": m.created_at} for m in messages]}


@app.post("/api/conversations/{conversation_id}/messages", status_code=202)
def send_message(conversation_id: str, body: MessageIn, owner: str = Depends(auth), db: Session = Depends(db_session)):
    if not owned_conversation(db, conversation_id, owner):
        raise HTTPException(404, "Conversación no encontrada")
    since = now() - timedelta(hours=1)
    count = db.scalar(select(func.count(Run.id)).join(Conversation).where(Conversation.owner_hash == owner, Run.created_at >= since))
    if count >= settings.rate_limit_per_hour:
        raise HTTPException(429, "Límite de uso por hora alcanzado")
    in_flight = db.scalar(select(func.count(Run.id)).join(Conversation).where(
        Conversation.owner_hash == owner, Run.status.in_(["queued", "running"])))
    if in_flight >= settings.max_queued_runs_per_owner:
        raise HTTPException(429, "Demasiados trabajos pendientes")
    message = Message(conversation_id=conversation_id, role="user", content=body.content)
    db.add(message)
    db.flush()
    run = Run(conversation_id=conversation_id, message_id=message.id)
    db.add(run)
    db.commit()
    return {"run_id": run.id, "status": run.status}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, owner: str = Depends(auth), db: Session = Depends(db_session)):
    run = owned_run(db, run_id, owner)
    if not run:
        raise HTTPException(404, "Ejecución no encontrada")
    return public_run(run)


@app.post("/api/runs/{run_id}/retry", status_code=202)
def retry_run(run_id: str, owner: str = Depends(auth), db: Session = Depends(db_session)):
    run = owned_run(db, run_id, owner)
    if not run:
        raise HTTPException(404, "Ejecución no encontrada")
    if run.status != "paused":
        raise HTTPException(409, "Solo se puede reintentar un trabajo pausado")
    run.status = "queued"
    run.error = None
    run.result = None
    run.attempts = 0
    run.updated_at = now()
    db.commit()
    return {"run_id": run.id, "status": run.status}


@app.get("/api/runs/{run_id}/traces")
def get_traces(run_id: str, owner: str = Depends(auth), db: Session = Depends(db_session)):
    if not owned_run(db, run_id, owner):
        raise HTTPException(404, "Ejecución no encontrada")
    traces = db.scalars(select(Trace).where(Trace.run_id == run_id).order_by(Trace.created_at, Trace.id)).all()
    return [{"stage": t.stage, "attempt": t.attempt, "duration_ms": t.duration_ms,
             "input": t.input, "output": t.output, "error": t.error, "created_at": t.created_at} for t in traces]


@app.get("/stream/{session_id}")
async def stream_run(session_id: str, run_id: str, owner: str = Depends(auth), db: Session = Depends(db_session)):
    run = owned_run(db, run_id, owner)
    if not run or run.conversation_id != session_id:
        raise HTTPException(404, "Ejecución no encontrada")

    async def events():
        seen = set()
        last_status = None
        terminal = {"approved", "answered", "needs_clarification", "exhausted", "technical_error", "paused"}
        while True:
            with SessionLocal() as current:
                run = owned_run(current, run_id, owner)
                if not run:
                    return
                traces = current.scalars(select(Trace).where(Trace.run_id == run_id)
                                         .order_by(Trace.created_at, Trace.id)).all()
                status = run.status
            if status != last_status:
                data = {"node": "Workflow", "status": status, "run_id": run_id}
                yield f"event: status\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                last_status = status
            for trace in traces:
                if trace.id in seen:
                    continue
                seen.add(trace.id)
                label = trace.stage.capitalize()
                detail = (trace.output or {}).get("summary") or trace.error or (
                    "Rechazado" if trace.stage == "reviewer" and
                    not (trace.output or {}).get("approved", True) else "Completado")
                if trace.stage == "reviewer":
                    detail += f" (Intento {trace.attempt}/{settings.max_coder_attempts})"
                data = {"node": label, "status": detail, "attempt": trace.attempt,
                        "duration_ms": trace.duration_ms}
                yield f"id: {trace.id}\nevent: trace\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            if status in terminal:
                return
            await asyncio.sleep(0.4)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/runs/{run_id}/files")
def get_files(run_id: str, owner: str = Depends(auth), db: Session = Depends(db_session)):
    run = owned_run(db, run_id, owner)
    if not run:
        raise HTTPException(404, "Ejecución no encontrada")
    versions = db.scalars(select(FileVersion).where(FileVersion.run_id == run_id).order_by(FileVersion.attempt, FileVersion.created_at)).all()
    return [{"attempt": f.attempt, "path": f.path, "content": f.content} for f in versions]
