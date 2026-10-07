import time
from pathlib import PurePosixPath
from typing import Literal, TypedDict

import httpx
from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field, ValidationError, model_validator

from .config import Settings


class AgentFailure(Exception):
    pass


class PlanOutput(BaseModel):
    kind: Literal["question", "code"]
    summary: str
    steps: list[str] = Field(default_factory=list)
    answer: str | None = None

    @model_validator(mode="after")
    def question_has_answer(self):
        if self.kind == "question" and not self.answer:
            raise ValueError("question requires answer")
        return self


class GeneratedFile(BaseModel):
    path: str
    content: str

    @model_validator(mode="after")
    def safe_path(self):
        path = PurePosixPath(self.path)
        if path.is_absolute() or not self.path or ".." in path.parts or "\\" in self.path:
            raise ValueError("unsafe path")
        return self


class CodeOutput(BaseModel):
    summary: str
    files: list[GeneratedFile] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_paths(self):
        if len({item.path for item in self.files}) != len(self.files):
            raise ValueError("duplicate file path")
        return self


class ReviewOutput(BaseModel):
    approved: bool
    summary: str
    feedback: list[str] = Field(default_factory=list)


PROMPTS = {
    "planner": "You are Planner. Decide whether the latest message asks a question about existing work or requests code. For questions, answer from provided history and files; do not request implementation. For code, state a concise implementable plan and reasonable assumptions. Return only JSON with kind, summary, steps, answer. Never reveal private reasoning.",
    "coder": "You are Coder. Implement the plan. Return only JSON with summary and files [{path,content}]. Files are a full snapshot of all deliverable files, preserving and modifying prior code as needed. Apply reviewer feedback if provided. Do not claim generated code was executed. Never reveal private reasoning.",
    "reviewer": "You are Reviewer. Inspect the proposed files against the user request and plan. Return only JSON with approved, summary, feedback. Approve only if sufficiently complete and coherent. This is a static review: generated code was not executed. Never reveal private reasoning.",
}


class ModelProvider:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.last_retries = 0

    def call(self, role: str, payload: dict) -> dict:
        self.last_retries = 0
        if not self.settings.model_api_key:
            raise AgentFailure("Modelo no configurado. Configure MODEL_API_KEY en el servidor.")
        body = {
            "model": self.settings.model_name,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": PROMPTS[role]},
                {"role": "user", "content": __import__("json").dumps(payload, ensure_ascii=False)},
            ],
        }
        for retry in range(self.settings.model_api_retries + 1):
            try:
                with httpx.Client(timeout=self.settings.model_timeout_seconds) as client:
                    response = client.post(
                        self.settings.model_base_url.rstrip("/") + "/chat/completions",
                        headers={"Authorization": f"Bearer {self.settings.model_api_key}"},
                        json=body,
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    raise httpx.HTTPStatusError("provider unavailable", request=response.request, response=response)
                response.raise_for_status()
                return __import__("json").loads(response.json()["choices"][0]["message"]["content"])
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                if retry == self.settings.model_api_retries or (isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in (429, 500, 502, 503, 504)):
                    raise AgentFailure("El proveedor del modelo no respondió correctamente o agotó los reintentos.") from exc
                self.last_retries += 1
                time.sleep(min(2 ** retry, 4))
            except (ValueError, KeyError, TypeError) as exc:
                raise AgentFailure("El modelo devolvió JSON inválido.") from exc
        raise AgentFailure("El proveedor del modelo no respondió.")


class GraphState(TypedDict, total=False):
    message: str
    history: list[dict]
    previous_files: list[dict]
    plan: dict
    code: dict
    review: dict
    attempts: int
    status: str
    result: str


def build_graph(provider, settings: Settings, record):
    def invoke(role, schema, payload, attempt=0):
        start = time.monotonic()
        try:
            parsed = schema.model_validate(provider.call(role, payload))
            output = parsed.model_dump()
            record(role, attempt, payload, output, None, int((time.monotonic() - start) * 1000))
            return output
        except (ValidationError, AgentFailure, ValueError) as exc:
            public_error = str(exc) if isinstance(exc, AgentFailure) else "El modelo devolvió una respuesta con estructura inválida."
            record(role, attempt, payload, {}, public_error, int((time.monotonic() - start) * 1000))
            raise AgentFailure(public_error) from exc

    def planner(state):
        plan = invoke("planner", PlanOutput, {
            "message": state["message"], "history": state["history"], "files": state["previous_files"]
        })
        result = {"plan": plan}
        if plan["kind"] == "question":
            result.update(status="answered", result=plan["answer"])
        return result

    def coder(state):
        attempt = state.get("attempts", 0) + 1
        code = invoke("coder", CodeOutput, {
            "message": state["message"], "plan": state["plan"],
            "previous_files": state["previous_files"], "prior_candidate": state.get("code"),
            "review_feedback": state.get("review", {}).get("feedback", []),
        }, attempt)
        return {"code": code, "attempts": attempt}

    def reviewer(state):
        review = invoke("reviewer", ReviewOutput, {
            "message": state["message"], "plan": state["plan"], "code": state["code"]
        }, state["attempts"])
        if review["approved"]:
            return {"review": review, "status": "approved", "result": state["code"]["summary"]}
        if state["attempts"] >= settings.max_coder_attempts:
            return {"review": review, "status": "exhausted", "result": "No se obtuvo aprobación tras agotar los intentos de corrección."}
        return {"review": review}

    graph = StateGraph(GraphState)
    graph.add_node("planner", planner)
    graph.add_node("coder", coder)
    graph.add_node("reviewer", reviewer)
    graph.set_entry_point("planner")
    graph.add_conditional_edges("planner", lambda s: "done" if s.get("status") else "code", {"done": END, "code": "coder"})
    graph.add_edge("coder", "reviewer")
    graph.add_conditional_edges("reviewer", lambda s: "done" if s.get("status") else "retry", {"done": END, "retry": "coder"})
    return graph.compile()
