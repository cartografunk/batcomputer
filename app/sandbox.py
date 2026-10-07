import ast
import json
import shlex
import time
from html.parser import HTMLParser
from pathlib import PurePosixPath

from e2b import CommandExitException, Sandbox

from .config import Settings


class InlineScripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.active = tag == "script" and "src" not in attrs and attrs.get("type", "") in ("", "text/javascript", "module")

    def handle_data(self, data):
        if self.active:
            self.scripts.append(data)

    def handle_endtag(self, tag):
        if tag == "script":
            self.active = False


class E2BValidator:
    """Runs generated files only in a disposable external sandbox."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def validate(self, files: list[dict]) -> dict:
        if not self.settings.e2b_api_key:
            started = time.monotonic()
            checks = []
            for item in files:
                path = item["path"]
                if not path.endswith((".py", ".json")):
                    continue
                try:
                    if path.endswith(".py"):
                        ast.parse(item["content"], filename=path)
                    else:
                        json.loads(item["content"])
                    checks.append({"name": f"syntax:{path}", "exit_code": 0, "stdout": "", "stderr": ""})
                except (SyntaxError, ValueError) as exc:
                    checks.append({"name": f"syntax:{path}", "exit_code": 1, "stdout": "",
                                   "stderr": str(exc)[:self.settings.sandbox_output_chars]})
            failed = any(check["exit_code"] for check in checks)
            return {"status": "failed" if failed else "not_executed",
                    "summary": ("Falló el análisis sintáctico local; no se ejecutó código." if failed else
                                "Análisis sintáctico local completado cuando aplica; código no ejecutado."),
                    "checks": checks, "duration_ms": int((time.monotonic() - started) * 1000)}
        has_code = any(item["path"].endswith((".py", ".js", ".mjs")) for item in files)
        if not has_code:
            for item in files:
                if item["path"].endswith(".html"):
                    parser = InlineScripts()
                    parser.feed(item["content"])
                    if parser.scripts:
                        has_code = True
                        break
        if not has_code:
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
            python_files = [item for item in files if item["path"].endswith(".py")]
            js_paths = [item["path"] for item in files if item["path"].endswith((".js", ".mjs"))]
            for index, item in enumerate(file for file in files if file["path"].endswith(".html")):
                parser = InlineScripts()
                parser.feed(item["content"])
                for script_index, script in enumerate(parser.scripts):
                    name = f"_inline_{index}_{script_index}.js"
                    sandbox.files.write("/home/user/project/" + name, script,
                                        request_timeout=self.settings.sandbox_timeout_seconds)
                    js_paths.append(name)
            commands = []
            if python_files:
                commands.append(("python_syntax", "python3 -m compileall -q /home/user/project"))
            for path in js_paths:
                commands.append((f"javascript_syntax:{path}",
                                 "node --check " + shlex.quote("/home/user/project/" + path)))
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
                    status = "error" if "not found" in check["stderr"].lower() else "failed"
                    result = {"status": status, "summary": f"Falló la validación {name} en el sandbox.", "checks": checks}
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
