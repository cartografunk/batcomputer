"""Small server-side Supabase Auth bridge and signed, short-lived app sessions."""

import base64
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import urlparse

import httpx

from .config import Settings


class LoginRejected(Exception):
    pass


def allowed_emails(settings: Settings) -> set[str]:
    return {email.strip().lower() for email in settings.allowed_user_emails.split(",") if email.strip()}


def sign_session(settings: Settings, role: str, subject: str, email: str = "") -> str:
    if role not in {"member", "guest"}:
        raise ValueError("invalid role")
    lifetime = 12 * 3600 if role == "member" else 3600
    claims = {"v": 2, "role": role, "sub": subject, "email": email.lower(), "exp": int(time.time()) + lifetime}
    payload = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode()).rstrip(b"=").decode()
    signature = hmac.new(settings.auth_tokens.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return payload + "." + signature


def read_session(settings: Settings, cookie: str | None) -> dict | None:
    if not cookie or len(cookie) > 2048:
        return None
    payload, separator, signature = cookie.rpartition(".")
    if not separator or not payload or not signature:
        return None
    expected = hmac.new(settings.auth_tokens.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(claims, dict) or claims.get("v") != 2 or claims.get("role") not in {"member", "guest"}:
        return None
    if not isinstance(claims.get("exp"), int) or claims["exp"] <= time.time():
        return None
    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        return None
    if claims["role"] == "member" and claims.get("email", "").lower() not in allowed_emails(settings):
        return None
    return claims


def authenticate_password(settings: Settings, email: str, password: str) -> dict:
    """Verify credentials with GoTrue; never log or retain the password/token."""
    email = email.strip().lower()
    if email not in allowed_emails(settings):
        raise LoginRejected()
    parsed = urlparse(settings.supabase_url)
    if parsed.scheme != "https" or not parsed.hostname or not settings.supabase_publishable_key:
        raise RuntimeError("Supabase Auth no configurado")
    try:
        response = httpx.post(
            settings.supabase_url.rstrip("/") + "/auth/v1/token",
            params={"grant_type": "password"},
            headers={"apikey": settings.supabase_publishable_key},
            json={"email": email, "password": password}, timeout=10,
        )
    except httpx.RequestError as exc:
        raise RuntimeError("Supabase Auth no disponible") from exc
    if response.status_code in {400, 401, 403, 422}:
        raise LoginRejected()
    if response.status_code != 200:
        raise RuntimeError("Supabase Auth no disponible")
    try:
        user = response.json()["user"]
        if user["email"].lower() != email or not user["id"]:
            raise LoginRejected()
        return {"id": user["id"], "email": email}
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise RuntimeError("Respuesta de Supabase Auth inválida") from exc


def guest_subject() -> str:
    return secrets.token_urlsafe(24)


def accept_invitation(settings: Settings, access_token: str, password: str) -> dict:
    """Verify an invite token and set the user's first password in Supabase."""
    if len(password) < 8 or len(password) > 1024 or not access_token or len(access_token) > 8192:
        raise LoginRejected()
    endpoint = settings.supabase_url.rstrip("/") + "/auth/v1/user"
    headers = {"apikey": settings.supabase_publishable_key, "Authorization": "Bearer " + access_token}
    try:
        current = httpx.get(endpoint, headers=headers, timeout=10)
        if current.status_code != 200:
            raise LoginRejected()
        user = current.json()
        email = user.get("email", "").lower()
        if email not in allowed_emails(settings) or not user.get("id"):
            raise LoginRejected()
        updated = httpx.put(endpoint, headers=headers, json={"password": password}, timeout=10)
    except httpx.RequestError as exc:
        raise RuntimeError("Supabase Auth no disponible") from exc
    if updated.status_code in {400, 401, 403, 422}:
        raise LoginRejected()
    if updated.status_code != 200:
        raise RuntimeError("Supabase Auth no disponible")
    return {"id": user["id"], "email": email}
