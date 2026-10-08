"""Proveedor simulado para `python -m evals --dry-run`.

Verifica que la batería corre de punta a punta sin gastar tokens. Sus puntajes no miden nada:
responde siempre lo mismo. Simula los fallos inducidos (clave inválida y timeout) para que
también se ejerciten esas rutas.
"""

from app.agents import AgentFailure, TemporaryProviderFailure

INVALID_KEY = "invalid-eval-key"


class DryRunProvider:
    def __init__(self, settings):
        self.settings = settings
        self.last_retries = 0
        self.usage = {}

    def describe(self):
        return {role: "dry-run" for role in ("router", "question", "planner", "coder", "reviewer")}

    def call(self, role, payload):
        self.last_retries = 0
        if self.settings.model_api_key == INVALID_KEY:
            raise AgentFailure("El proveedor del modelo rechazó la solicitud (HTTP 401).")
        if self.settings.model_timeout_seconds < 0.01:
            raise TemporaryProviderFailure("Fallo temporal del proveedor de IA. Tu estado está guardado. Intenta de nuevo.")
        entry = self.usage.setdefault("dry-run", {"calls": 0, "input_tokens": 0, "output_tokens": 0})
        entry["calls"] += 1
        if role == "router":
            message = payload["message"]
            kind = ("QUESTION" if "?" in message else
                    "MODIFICATION" if payload["has_previous_files"] else "NEW_TICKET")
            return {"kind": kind, "reason": "Simulado"}
        if role == "question":
            return {"answer": "Respuesta simulada."}
        if role == "planner":
            return {"kind": "code", "summary": "Plan simulado", "criteria": [
                {"description": "Entregar un archivo", "verification": "Inspección"}]}
        if role == "coder":
            files = [file for file in payload["previous_files"] if file["path"] != "README.md"]
            return {"summary": "Código simulado",
                    "files": files or [{"path": "main.py", "content": "print('simulado')\n"}]}
        if role == "reviewer":
            return {"approved": True, "summary": "Aprobado (simulado)", "feedback": []}
        raise AgentFailure(f"Rol desconocido: {role}")
