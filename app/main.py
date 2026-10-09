import asyncio
import hashlib
import hmac
import json
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .config import settings
from .access import LoginRejected, accept_invitation, authenticate_password, guest_subject, read_session, sign_session
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
        if settings.auth_mode == "supabase" and (
            not settings.supabase_url.startswith("https://") or not settings.supabase_publishable_key or
            not settings.allowed_user_emails.strip()
        ):
            raise RuntimeError("Production requires Supabase Auth URL, publishable key and allowed emails")
        if settings.model_mode in {"azure", "azure_responses"} and (
            not settings.azure_openai_endpoint.startswith("https://") or
            not settings.azure_openai_deployment.strip() or
            not settings.azure_openai_api_version.strip()
        ):
            raise RuntimeError("Production requires Azure OpenAI endpoint, deployment and API version")
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


SESSION_COOKIE = "batcomputer_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30


def cookie_token(value: str | None) -> str | None:
    if not value or len(value) > 256:
        return None
    token, separator, signature = value.rpartition(".")
    if not separator or not token or not signature:
        return None
    expected = hmac.new(settings.auth_tokens.encode(), token.encode(), hashlib.sha256).hexdigest()
    return token if hmac.compare_digest(signature, expected) else None


def auth(request: Request, authorization: str = Header(default="")):
    if settings.auth_mode == "supabase":
        claims = read_session(settings, request.cookies.get(SESSION_COOKIE))
        if claims is None:
            raise HTTPException(401, "Inicia sesión para continuar")
        request.state.access_role = claims["role"]
        prefix = "user:" if claims["role"] == "member" else "guest:"
        return owner_hash(prefix + claims["sub"])
    token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
    if authorization:
        valid = any(secrets.compare_digest(token, configured.strip()) for configured in settings.auth_tokens.split(",") if configured.strip())
        if not valid:
            raise HTTPException(401, "Token de acceso inválido")
        return owner_hash(token)
    session_token = cookie_token(request.cookies.get(SESSION_COOKIE))
    if session_token is None:
        raise HTTPException(401, "Sesión no disponible")
    return owner_hash(session_token)


class MessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=20000)


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=1024)


class InviteIn(BaseModel):
    access_token: str = Field(min_length=1, max_length=8192)
    password: str = Field(min_length=8, max_length=1024)


def set_access_cookie(response: Response, value: str, max_age: int):
    response.set_cookie(SESSION_COOKIE, value, max_age=max_age, httponly=True,
                        secure=settings.app_env == "production", samesite="lax", path="/")
    response.headers["Cache-Control"] = "no-store"


@app.get("/")
def home():
    return FileResponse(frontend / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/session")
def ensure_session(request: Request, response: Response):
    if settings.auth_mode == "supabase":
        claims = read_session(settings, request.cookies.get(SESSION_COOKIE))
        response.headers["Cache-Control"] = "no-store"
        return {"authenticated": bool(claims), "role": claims["role"] if claims else None,
                "email": claims.get("email") if claims and claims["role"] == "member" else None}
    token = cookie_token(request.cookies.get(SESSION_COOKIE))
    if token is None:
        token = secrets.token_urlsafe(32)
        signature = hmac.new(settings.auth_tokens.encode(), token.encode(), hashlib.sha256).hexdigest()
        response.set_cookie(SESSION_COOKIE, f"{token}.{signature}", max_age=SESSION_MAX_AGE,
                            httponly=True, secure=settings.app_env == "production", samesite="lax")
    response.headers["Cache-Control"] = "no-store"
    return {"status": "ready"}


@app.post("/api/auth/login")
def login(body: LoginIn, response: Response):
    if settings.auth_mode != "supabase":
        raise HTTPException(404)
    try:
        user = authenticate_password(settings, body.email, body.password)
    except LoginRejected:
        raise HTTPException(401, "Correo o contraseña incorrectos") from None
    except RuntimeError:
        raise HTTPException(503, "El acceso no está disponible temporalmente") from None
    set_access_cookie(response, sign_session(settings, "member", user["id"], user["email"]), 12 * 3600)
    return {"authenticated": True, "role": "member", "email": user["email"]}


@app.post("/api/auth/accept-invite")
def accept_invite(body: InviteIn, response: Response):
    if settings.auth_mode != "supabase":
        raise HTTPException(404)
    try:
        user = accept_invitation(settings, body.access_token, body.password)
    except LoginRejected:
        raise HTTPException(401, "La invitación expiró o la contraseña no es válida") from None
    except RuntimeError:
        raise HTTPException(503, "El acceso no está disponible temporalmente") from None
    set_access_cookie(response, sign_session(settings, "member", user["id"], user["email"]), 12 * 3600)
    return {"authenticated": True, "role": "member", "email": user["email"]}


@app.post("/api/auth/guest")
def guest_login(response: Response):
    if settings.auth_mode != "supabase":
        raise HTTPException(404)
    set_access_cookie(response, sign_session(settings, "guest", guest_subject()), 3600)
    return {"authenticated": True, "role": "guest", "email": None}


@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True,
                           secure=settings.app_env == "production", samesite="lax")
    response.headers["Cache-Control"] = "no-store"
    return {"authenticated": False}


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


@app.get("/api/conversations")
def list_conversations(owner: str = Depends(auth), db: Session = Depends(db_session)):
    conversations = db.scalars(select(Conversation).where(Conversation.owner_hash == owner)
                               .order_by(Conversation.created_at.desc(), Conversation.id.desc()).limit(20)).all()
    return [{"id": item.id, "created_at": item.created_at} for item in conversations]


@app.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: str, owner: str = Depends(auth), db: Session = Depends(db_session)):
    conversation = owned_conversation(db, conversation_id, owner)
    if not conversation:
        raise HTTPException(404, "Conversación no encontrada")
    messages = db.scalars(select(Message).where(Message.conversation_id == conversation_id).order_by(Message.created_at, Message.id)).all()
    last_run = db.scalar(select(Run).where(Run.conversation_id == conversation_id)
                         .order_by(Run.created_at.desc(), Run.id.desc()).limit(1))
    return {"id": conversation.id, "created_at": conversation.created_at,
            "pending_clarification": conversation.pending_clarification,
            "last_run": public_run(last_run) if last_run else None,
            "messages": [{"id": m.id, "role": m.role, "content": m.content, "created_at": m.created_at} for m in messages]}


@app.post("/api/conversations/{conversation_id}/messages", status_code=202)
def send_message(conversation_id: str, body: MessageIn, request: Request,
                 owner: str = Depends(auth), db: Session = Depends(db_session)):
    if not owned_conversation(db, conversation_id, owner):
        raise HTTPException(404, "Conversación no encontrada")
    since = now() - timedelta(hours=1)
    count = db.scalar(select(func.count(Run.id)).join(Conversation).where(Conversation.owner_hash == owner, Run.created_at >= since))
    is_guest = settings.auth_mode == "supabase" and request.state.access_role == "guest"
    own_limit = settings.guest_rate_limit_per_hour if is_guest else settings.rate_limit_per_hour
    if count >= own_limit:
        raise HTTPException(429, "Límite de uso por hora alcanzado")
    if is_guest:
        guest_count = db.scalar(select(func.count(Run.id)).join(Conversation).where(
            Conversation.owner_hash.like("g%"), Run.created_at >= since))
        if guest_count >= settings.guest_global_rate_limit_per_hour:
            raise HTTPException(429, "Cupo de invitado agotado por esta hora")
    global_count = db.scalar(select(func.count(Run.id)).where(Run.created_at >= since))
    if global_count >= settings.global_rate_limit_per_hour:
        raise HTTPException(429, "Límite global de uso por hora alcanzado")
    in_flight = db.scalar(select(func.count(Run.id)).join(Conversation).where(
        Conversation.owner_hash == owner, Run.status.in_(["queued", "running"])))
    if in_flight >= (1 if is_guest else settings.max_queued_runs_per_owner):
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
