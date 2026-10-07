from app.db import make_engine


def test_postgres_connection_uses_batcomputer_schema(monkeypatch):
    calls = {}
    expected_engine = object()

    def fake_create_engine(url, **kwargs):
        calls.update(url=url, kwargs=kwargs)
        return expected_engine

    def fake_listens_for(engine, name):
        assert engine is expected_engine
        assert name == "connect"

        def register(callback):
            calls["callback"] = callback
            return callback

        return register

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def execute(self, query):
            calls["query"] = query

    class Connection:
        def cursor(self):
            return Cursor()

        def commit(self):
            calls["committed"] = True

    monkeypatch.setattr("app.db.create_engine", fake_create_engine)
    monkeypatch.setattr("app.db.event.listens_for", fake_listens_for)
    engine = make_engine("postgresql+psycopg://example.invalid/batcomputer")
    calls["callback"](Connection(), None)

    assert engine is expected_engine
    assert calls["query"] == "SET SESSION search_path TO batcomputer"
    assert calls["committed"] is True
