from types import SimpleNamespace

from e2b import CommandExitException

from app.config import Settings
from app.sandbox import E2BValidator


class FakeFiles:
    def __init__(self):
        self.writes = []

    def write(self, path, content, **_):
        self.writes.append((path, content))


class FakeCommands:
    def __init__(self, failure=False):
        self.calls = []
        self.failure = failure

    def run(self, command, **options):
        self.calls.append((command, options))
        if self.failure:
            raise CommandExitException(stderr="SyntaxError", stdout="", exit_code=1, error=None)
        return SimpleNamespace(exit_code=0, stdout="ok", stderr="")


class FakeSandbox:
    created = []
    instances = []
    failure = False

    @classmethod
    def create(cls, **options):
        cls.created.append(options)
        instance = cls()
        instance.files = FakeFiles()
        instance.commands = FakeCommands(cls.failure)
        instance.killed = False
        cls.instances.append(instance)
        return instance

    def kill(self, **_):
        self.killed = True


def test_sandbox_isolated_network_off_and_cleanup(monkeypatch):
    import app.sandbox as module
    monkeypatch.setattr(module, "Sandbox", FakeSandbox)
    FakeSandbox.created.clear()
    FakeSandbox.instances.clear()
    FakeSandbox.failure = False
    settings = Settings(e2b_api_key="fake-key", sandbox_timeout_seconds=12)
    outcome = E2BValidator(settings).validate([{"path": "main.py", "content": "print(1)"}])
    assert outcome["status"] == "passed"
    assert FakeSandbox.created[-1]["allow_internet_access"] is False
    assert FakeSandbox.created[-1]["envs"] == {}
    assert FakeSandbox.created[-1]["api_key"] == "fake-key"
    assert FakeSandbox.instances[-1].killed
    assert FakeSandbox.instances[-1].files.writes == [("/home/user/project/main.py", "print(1)")]


def test_sandbox_code_failure_distinct_from_infrastructure(monkeypatch):
    import app.sandbox as module
    monkeypatch.setattr(module, "Sandbox", FakeSandbox)
    FakeSandbox.failure = True
    result = E2BValidator(Settings(e2b_api_key="fake-key")).validate([{"path": "bad.py", "content": "broken"}])
    assert result["status"] == "failed"
    assert result["checks"][0]["stderr"] == "SyntaxError"
    FakeSandbox.failure = False

    class DownSandbox:
        @staticmethod
        def create(**_):
            raise RuntimeError("secret from SDK must not leak")

    monkeypatch.setattr(module, "Sandbox", DownSandbox)
    result = E2BValidator(Settings(e2b_api_key="fake-key")).validate([{"path": "a.py", "content": "x"}])
    assert result["status"] == "error"
    assert "secret" not in result["summary"]


def test_without_key_is_not_executed():
    result = E2BValidator(Settings(e2b_api_key="")).validate([{"path": "main.py", "content": "x"}])
    assert result["status"] == "not_executed"


def test_non_python_is_not_claimed_validated():
    result = E2BValidator(Settings(e2b_api_key="fake-key")).validate([{"path": "index.html", "content": "<h1>Hello</h1>"}])
    assert result["status"] == "not_executed"
