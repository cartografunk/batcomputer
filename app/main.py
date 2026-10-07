import asyncio
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .db import Base, Conversation, FileVersion, Message, Run, SessionLocal, Trace, engine, now
from .service import claim_next, latest_files, owned_conversation, owned_run, owner_hash, process_run, public_run


async def worker_loop():
    while True:
        try:
            with SessionLocal() as db:
                claimed = claim_next(db, settings)
            if claimed:
                run_id, lease_id = claimed
                await asyncio.to_thread(process_run, SessionLocal, run_id, settings, lease_id=lease_id)
            else:
                await asyncio.sleep(settings.worker_poll_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Database outages remain queued/running; stale jobs are reclaimed after recovery.
            await asyncio.sleep(settings.worker_poll_seconds)


@asynccontextmanager
async def lifespan(_app):
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


app = FastAPI(title="Ticket to Code Agents", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[x.strip() for x in settings.cors_origins.split(",") if x.strip()],
                   allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"])


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
    return FileResponse("app/static/index.html")


@app.get("/flappy")
def flappy():
    return FileResponse("app/static/flappy.html")


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
            "messages": [{"id": m.id, "role": m.role, "content": m.content, "created_at": m.created_at} for m in messages]}


@app.post("/api/conversations/{conversation_id}/messages", status_code=202)
def send_message(conversation_id: str, body: MessageIn, owner: str = Depends(auth), db: Session = Depends(db_session)):
    if not owned_conversation(db, conversation_id, owner):
        raise HTTPException(404, "Conversación no encontrada")
    since = now() - timedelta(hours=1)
    count = db.scalar(select(func.count(Run.id)).join(Conversation).where(Conversation.owner_hash == owner, Run.created_at >= since))
    if count >= settings.rate_limit_per_hour:
        raise HTTPException(429, "Límite de uso por hora alcanzado")
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


@app.get("/api/runs/{run_id}/traces")
def get_traces(run_id: str, owner: str = Depends(auth), db: Session = Depends(db_session)):
    if not owned_run(db, run_id, owner):
        raise HTTPException(404, "Ejecución no encontrada")
    traces = db.scalars(select(Trace).where(Trace.run_id == run_id).order_by(Trace.created_at, Trace.id)).all()
    return [{"stage": t.stage, "attempt": t.attempt, "duration_ms": t.duration_ms,
             "input": t.input, "output": t.output, "error": t.error, "created_at": t.created_at} for t in traces]


@app.get("/api/runs/{run_id}/files")
def get_files(run_id: str, owner: str = Depends(auth), db: Session = Depends(db_session)):
    run = owned_run(db, run_id, owner)
    if not run:
        raise HTTPException(404, "Ejecución no encontrada")
    versions = db.scalars(select(FileVersion).where(FileVersion.run_id == run_id).order_by(FileVersion.attempt, FileVersion.created_at)).all()
    return [{"attempt": f.attempt, "path": f.path, "content": f.content} for f in versions]
