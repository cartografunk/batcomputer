import time
from pathlib import PurePosixPath

from e2b import CommandExitException, Sandbox

from .config import Settings


class E2BValidator:
    """Runs generated files only in a disposable external sandbox."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def validate(self, files: list[dict]) -> dict:
        if not self.settings.e2b_api_key:
            return {"status": "not_executed", "summary": "Sandbox no configurado; código no ejecutado.",
                    "checks": [], "duration_ms": 0}
        if not any(item["path"].endswith(".py") for item in files):
            return {"status": "not_executed", "summary": "Sin comprobador automático para estos archivos; código no ejecutado en E2B.",
                    "checks": [], "duration_ms": 0}
        started = time.monotonic()
        sandbox = None
        checks = []
        result = {"status": "error", "summary": "No se pudo completar la validación aislada.", "checks": checks}
        try:
            sandbox = Sandbox.create(
                timeout=self.settings.sandbox_timeout_seconds,
                allow_internet_access=False,
                secure=True,
                envs={},
                api_key=self.settings.e2b_api_key,
                request_timeout=self.settings.sandbox_timeout_seconds,
            )
            for item in files:
                path = PurePosixPath(item["path"])
                if path.is_absolute() or ".." in path.parts or "\\" in item["path"]:
                    raise ValueError("unsafe generated path")
                sandbox.files.write("/home/user/project/" + item["path"], item["content"],
                                    request_timeout=self.settings.sandbox_timeout_seconds)
            commands = [("python_syntax", "python3 -m compileall -q /home/user/project")]
            if any(PurePosixPath(item["path"]).name.startswith("test") and item["path"].endswith(".py") for item in files):
                commands.append(("unit_tests", "python3 -m unittest discover -s /home/user/project -p 'test*.py'"))
            for name, command in commands:
                try:
                    completed = sandbox.commands.run(command, cwd="/home/user/project",
                                                     timeout=self.settings.sandbox_timeout_seconds,
                                                     request_timeout=self.settings.sandbox_timeout_seconds)
                    check = {"name": name, "exit_code": completed.exit_code,
                             "stdout": completed.stdout[:self.settings.sandbox_output_chars],
                             "stderr": completed.stderr[:self.settings.sandbox_output_chars]}
                except CommandExitException as exc:
                    check = {"name": name, "exit_code": exc.exit_code,
                             "stdout": exc.stdout[:self.settings.sandbox_output_chars],
                             "stderr": exc.stderr[:self.settings.sandbox_output_chars]}
                checks.append(check)
                if check["exit_code"] != 0:
                    result = {"status": "failed", "summary": f"Falló la validación {name} en el sandbox.", "checks": checks}
                    break
            else:
                result = {"status": "passed", "summary": "Validación aislada completada.", "checks": checks}
        except Exception:
            # SDK errors are intentionally not exposed: transport errors may contain URLs or headers.
            result = {"status": "error", "summary": "Error de infraestructura del sandbox; el código no se considera incorrecto por ello.", "checks": checks}
        finally:
            if sandbox is not None:
                try:
                    sandbox.kill(request_timeout=10)
                except Exception:
                    result = {"status": "error", "summary": "No se pudo confirmar el cierre del sandbox.", "checks": checks}
            result["duration_ms"] = int((time.monotonic() - started) * 1000)
        return result
