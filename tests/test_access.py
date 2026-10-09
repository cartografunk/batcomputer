import httpx
from fastapi.testclient import TestClient

from app.access import LoginRejected, accept_invitation, authenticate_password, read_session, sign_session
from app.config import Settings
from app.db import Base, make_engine
from app.service import owner_hash


def config(**kwargs):
    return Settings(_env_file=None, auth_tokens="test-signing-key", auth_mode="supabase",
                    supabase_url="https://example.supabase.co", supabase_publishable_key="sb_publishable_test",
                    **kwargs)


def test_password_signin_checks_supabase_and_allowlist(monkeypatch):
    seen = []

    def post(url, **kwargs):
        seen.append((url, kwargs))
        return httpx.Response(200, json={"user": {"id": "user-123", "email": "cartografunk@gmail.com"}})

    monkeypatch.setattr(httpx, "post", post)
    user = authenticate_password(config(), "Cartografunk@gmail.com", "private-password")
    assert user == {"id": "user-123", "email": "cartografunk@gmail.com"}
    assert seen[0][0] == "https://example.supabase.co/auth/v1/token"
    assert seen[0][1]["params"] == {"grant_type": "password"}
    assert seen[0][1]["json"]["password"] == "private-password"
    with __import__("pytest").raises(LoginRejected):
        authenticate_password(config(), "other@example.com", "anything")
    assert len(seen) == 1


def test_invitation_verifies_identity_before_setting_password(monkeypatch):
    calls = []

    def get(url, **kwargs):
        calls.append(("get", url, kwargs))
        return httpx.Response(200, json={"id": "user-123", "email": "iramjuarez98@hotmail.com"})

    def put(url, **kwargs):
        calls.append(("put", url, kwargs))
        return httpx.Response(200, json={"id": "user-123"})

    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(httpx, "put", put)
    assert accept_invitation(config(), "invite-token", "strong-password")["id"] == "user-123"
    assert calls[0][2]["headers"]["Authorization"] == "Bearer invite-token"
    assert calls[1][2]["json"] == {"password": "strong-password"}
    with __import__("pytest").raises(LoginRejected):
        accept_invitation(config(), "invite-token", "admin")


def test_signed_sessions_expire_and_allowlist_applies(monkeypatch):
    import app.access as access

    monkeypatch.setattr(access.time, "time", lambda: 1000)
    token = sign_session(config(), "member", "user-123", "cartografunk@gmail.com")
    assert read_session(config(), token)["sub"] == "user-123"
    assert read_session(config(allowed_user_emails="iramjuarez98@hotmail.com"), token) is None
    assert read_session(config(), token + "x") is None
    monkeypatch.setattr(access.time, "time", lambda: 1000 + 12 * 3600)
    assert read_session(config(), token) is None
    assert owner_hash("guest:abc").startswith("g")
    assert owner_hash("user:abc").startswith("u")


def test_guest_isolation_and_shared_quota(tmp_path, monkeypatch):
    import app.main as main
    from sqlalchemy.orm import sessionmaker

    engine = make_engine(f"sqlite:///{tmp_path / 'access.db'}")
    Base.metadata.create_all(engine)
    store = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(main, "SessionLocal", store)
    for name, value in {
        "auth_tokens": "test-signing-key", "auth_mode": "supabase",
        "guest_rate_limit_per_hour": 3, "guest_global_rate_limit_per_hour": 1,
        "global_rate_limit_per_hour": 100,
    }.items():
        monkeypatch.setattr(main.settings, name, value)

    visitor = TestClient(main.app)
    assert visitor.get("/api/session").json()["authenticated"] is False
    assert visitor.post("/api/conversations").status_code == 401
    assert visitor.post("/api/auth/guest").json()["role"] == "guest"
    first = visitor.post("/api/conversations").json()["id"]
    assert visitor.post(f"/api/conversations/{first}/messages", json={"content": "test"}).status_code == 202
    assert [item["id"] for item in visitor.get("/api/conversations").json()] == [first]
    assert visitor.get(f"/api/conversations/{first}").json()["last_run"]["status"] == "queued"

    another = TestClient(main.app)
    another.post("/api/auth/guest")
    assert another.get(f"/api/conversations/{first}").status_code == 404
    assert another.get("/api/conversations").json() == []
    second = another.post("/api/conversations").json()["id"]
    blocked = another.post(f"/api/conversations/{second}/messages", json={"content": "test"})
    assert blocked.status_code == 429
    assert another.post("/api/auth/logout").json() == {"authenticated": False}
    assert another.get("/api/session").json()["authenticated"] is False


def test_login_issues_member_session_and_rejects_legacy_cookie(tmp_path, monkeypatch):
    import app.main as main
    from sqlalchemy.orm import sessionmaker

    engine = make_engine(f"sqlite:///{tmp_path / 'login.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "SessionLocal", sessionmaker(engine, expire_on_commit=False))
    monkeypatch.setattr(main.settings, "auth_mode", "supabase")
    monkeypatch.setattr(main.settings, "auth_tokens", "test-signing-key")
    monkeypatch.setattr(main, "authenticate_password", lambda _settings, email, _password:
                        {"id": "user-123", "email": email.lower()})
    client = TestClient(main.app)
    client.cookies.set("batcomputer_session", "old.anonymous-cookie")
    assert client.post("/api/conversations").status_code == 401
    login = client.post("/api/auth/login", json={"email": "cartografunk@gmail.com", "password": "example-strong"})
    assert login.status_code == 200
    assert "httponly" in login.headers["set-cookie"].lower()
    assert client.get("/api/session").json()["email"] == "cartografunk@gmail.com"
    assert client.post("/api/conversations").status_code == 201
