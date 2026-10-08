"""Pruebas de la batería de evaluación (evals/). Sin red: proveedor simulado o HTTP interceptado."""

import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest

from app.agents import PROMPTS
from app.config import Settings
from app.lc_provider import LangChainProvider
from evals.__main__ import main, run_local
from evals.dryrun import DryRunProvider
from evals.langsmith_eval import run_langsmith, sync_dataset
from evals.runner import run_scenario
from evals.scenarios import load_scenarios
from evals.scoring import JUDGE_PROMPT, Judge, cost_usd, deterministic_checks

ROLE_BY_PROMPT = {prompt: role for role, prompt in PROMPTS.items()}
ANSWERS = {
    "router": {"kind": "NEW_TICKET", "reason": "Nuevo"},
    "planner": {"kind": "code", "summary": "Plan", "criteria": [{"description": "Validar cupón", "verification": "Prueba"}]},
    "coder": {"summary": "Cupones", "files": [{"path": "coupons.py", "content": "def validate_coupon(code):\n    expired = False\n"}]},
    "reviewer": {"approved": True, "summary": "Cumple", "feedback": []},
    "judge": {"score": 0.75, "verdict": "parcial", "reasoning": "Falta la prueba del monto."},
}


@pytest.fixture
def fake_openai(monkeypatch):
    def send(client, request, **_):
        body = json.loads(request.content)
        system = body["messages"][0]["content"]
        role = "judge" if system == JUDGE_PROMPT else ROLE_BY_PROMPT[system]
        data = {"id": "c", "object": "chat.completion", "created": 0, "model": body["model"],
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": json.dumps(ANSWERS[role])}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}
        return httpx2.Response(200, json=data, request=request)

    monkeypatch.setattr(httpx2.Client, "send", send)
    monkeypatch.setattr(time, "sleep", lambda _: None)


def base_settings(**overrides):
    values = {"model_api_key": "test-only", "model_name": "main-model", "e2b_api_key": "", "tavily_api_key": ""}
    values.update(overrides)
    return Settings(_env_file=None, **values)


def by_key(feedback):
    return {item["key"]: item for item in feedback}


def test_scenarios_file_is_valid_and_complete():
    scenarios = load_scenarios()
    ids = [scenario.id for scenario in scenarios]
    assert len(ids) == 10 and ids[0] == "S1-cupones"
    assert {s.failure for s in scenarios if s.failure} == {"auth", "timeout"}
    assert load_scenarios(only=["S3-pregunta"])[0].expected_route == "QUESTION"
    with pytest.raises(ValueError):
        load_scenarios(only=["no-existe"])


def test_invalid_scenario_is_rejected(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text('[[scenario]]\nid = "x"\ntitle = "x"\nturns = ["hola"]\nexpected_status = ["listo"]\nrubric = "r"\n',
                   encoding="utf-8")
    with pytest.raises(ValueError, match="expected_status"):
        load_scenarios(bad)


def test_dry_run_battery_exercises_every_path():
    rows = run_local(load_scenarios(), base_settings(), DryRunProvider, None, {}, 1)
    results = {row["scenario"]: row for row in rows}
    assert not [row for row in rows if row.get("error")]
    assert by_key(results["S2-seguimiento"]["feedback"])["usa_trabajo_previo"]["score"] == 1
    assert by_key(results["S3-pregunta"]["feedback"])["sin_codigo_nuevo"]["score"] == 1
    assert results["S9a-fallo-autenticacion"]["outputs"]["final"]["status"] == "technical_error"
    assert results["S9b-fallo-timeout"]["outputs"]["final"]["status"] == "paused"
    for scenario in ("S9a-fallo-autenticacion", "S9b-fallo-timeout"):
        assert by_key(results[scenario]["feedback"])["mensaje_de_error_claro"]["score"] == 1
    # S6 siembra una entrega agotada: la métrica existe y documenta si el Coder recibió ese código.
    assert "usa_trabajo_previo" in by_key(results["S6-tras-agotamiento"]["feedback"])


def test_checks_patterns_cost_and_missing_prices():
    final = {"status": "approved", "route": "NEW_TICKET", "files": [
        {"path": "a.py", "content": "coupon expired", "truncated": False, "chars": 14},
        {"path": "README.md", "content": "doc", "truncated": False, "chars": 3}],
        "coder_inputs": [{"attempt": 1, "previous_files": 0, "review_feedback": 0}], "attempts": 2,
        "validation_status": "not_executed", "result": "ok", "duration_s": 1.5}
    outputs = {"turns": [{**final, "usage": {"anthropic:claude-haiku-5-5": {"calls": 2, "input_tokens": 1_000_000,
                                                                            "output_tokens": 100_000}}}], "final": final}
    reference = {"expected_status": ["approved"], "expected_route": "NEW_TICKET", "expect_files": True,
                 "file_patterns": ["coupon", "user"], "check_previous_work": True}
    checks = by_key(deterministic_checks(outputs, reference, {"claude-haiku-5-5": {"input": 0.10, "output": 0.50}}))
    assert checks["estado_esperado"]["score"] == 1 and checks["ruta_esperada"]["score"] == 1
    assert checks["patrones_en_codigo"]["score"] == 0.5
    assert checks["usa_trabajo_previo"]["score"] == 0 and "D1" in checks["usa_trabajo_previo"]["comment"]
    assert checks["aprobado_primer_intento"]["score"] == 0
    assert checks["validacion_ejecutada"]["value"] == "not_executed"
    assert checks["costo_usd"]["score"] == pytest.approx(0.15)
    assert cost_usd({"remote:otro": {"input_tokens": 5, "output_tokens": 1}}, {}) == (None, ["remote:otro"])


def test_real_provider_path_counts_tokens_and_judges(fake_openai):
    settings = base_settings(model_backend="langchain", judge_model="openai:judge-model")
    scenario = load_scenarios(only=["S1-cupones"])[0]
    outputs = run_scenario(scenario, settings, LangChainProvider)
    assert outputs["final"]["status"] == "approved"
    assert outputs["models"]["coder"] == "remote:main-model"
    checks = by_key(deterministic_checks(outputs, scenario.expectations(), {"main-model": {"input": 1.0, "output": 2.0}}))
    assert checks["tokens_entrada"]["score"] == 400 and checks["tokens_salida"]["score"] == 80
    assert checks["costo_usd"]["score"] == pytest.approx(0.00056)
    judge = Judge(settings)
    verdict = judge(outputs, scenario.expectations())
    assert verdict == {"key": "juez_llm", "score": 0.75, "comment": "parcial: Falta la prueba del monto."}
    assert judge(outputs, {"judge": False, "rubric": ""}) is None


class Results(list):
    experiment_name = "exp-1"


class FakeLangSmith:
    def __init__(self):
        self.examples, self.updated, self.evaluated = {}, [], None

    def has_dataset(self, dataset_name):
        return bool(self.examples)

    def read_dataset(self, dataset_name):
        return SimpleNamespace(id="ds", name=dataset_name)

    def create_dataset(self, name, description=None):
        return SimpleNamespace(id="ds", name=name)

    def list_examples(self, dataset_id):
        return list(self.examples.values())

    def create_examples(self, dataset_id, examples):
        for item in examples:
            sid = item["inputs"]["scenario_id"]
            self.examples[sid] = SimpleNamespace(id=f"ex-{sid}", **item)

    def update_example(self, example_id, inputs, outputs, metadata):
        self.updated.append(example_id)
        self.examples[inputs["scenario_id"]] = SimpleNamespace(id=example_id, inputs=inputs, outputs=outputs,
                                                               metadata=metadata)

    def evaluate(self, target, data, evaluators, **kwargs):
        self.evaluated = kwargs
        rows = []
        for example in data:
            outputs = target(example.inputs)
            results = []
            for evaluator in evaluators:
                value = evaluator(outputs=outputs, reference_outputs=example.outputs)
                for item in value if isinstance(value, list) else [value]:
                    results.append(SimpleNamespace(key=item["key"], score=item.get("score"), value=item.get("value"),
                                                   comment=item.get("comment")))
            rows.append({"run": SimpleNamespace(outputs=outputs, error=None), "example": example,
                         "evaluation_results": {"results": results}})
        return Results(rows)


def test_langsmith_dataset_sync_is_idempotent():
    client, scenarios = FakeLangSmith(), load_scenarios()
    assert sync_dataset(client, scenarios)[1:] == (10, 0)
    assert sync_dataset(client, scenarios)[1:] == (0, 0)
    changed = [s if s.id != "S4-ambiguo" else type(s)(**{**s.__dict__, "title": "Nuevo título"}) for s in scenarios]
    assert sync_dataset(client, changed)[1:] == (0, 1)


def test_run_langsmith_glue():
    client = FakeLangSmith()
    all_scenarios = load_scenarios()
    selected = load_scenarios(only=["S1-cupones", "S9a-fallo-autenticacion"])
    outcome = run_langsmith(all_scenarios, selected, base_settings(), DryRunProvider, None, {}, 1, "prefijo",
                            {"modo": "prueba"}, client=client)
    assert outcome["experiment"] == "exp-1" and outcome["created"] == 10
    assert [row["scenario"] for row in outcome["rows"]] == ["S1-cupones", "S9a-fallo-autenticacion"]
    assert by_key(outcome["rows"][0]["feedback"])["estado_esperado"]["score"] == 1
    assert client.evaluated["experiment_prefix"] == "prefijo" and client.evaluated["max_concurrency"] == 1


def test_cli_dry_run_writes_reports(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)  # sin .env local
    assert main(["--dry-run", "--scenarios", "S1-cupones,S3-pregunta", "--out", str(tmp_path)]) == 0
    written = sorted(path.suffix for path in Path(tmp_path).iterdir())
    assert written == [".json", ".md"]
    assert "Resultados de la batería" in capsys.readouterr().out


def test_cli_langsmith_requires_key(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # sin .env local
    monkeypatch.setenv("LANGSMITH_API_KEY", "")
    assert main(["--dry-run", "--langsmith", "--scenarios", "S1-cupones", "--out", str(tmp_path)]) == 2
