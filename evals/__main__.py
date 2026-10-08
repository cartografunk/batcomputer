"""CLI de la batería: python -m evals --help"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from app.agents import AgentFailure
from app.config import Settings
from app.lc_provider import make_provider
from app.observability import configure_tracing

from .dryrun import DryRunProvider
from .langsmith_eval import run_langsmith
from .report import summarize
from .runner import describe_models, run_scenario
from .scenarios import DEFAULT_PATH, load_scenarios
from .scoring import Judge, deterministic_checks, load_prices


def run_local(scenarios, settings, provider_factory, judge, prices, repetitions):
    rows, total, index = [], len(scenarios) * repetitions, 0
    for repetition in range(1, repetitions + 1):
        for scenario in scenarios:
            index += 1
            print(f"[{index}/{total}] {scenario.id} (repetición {repetition})", file=sys.stderr, flush=True)
            row = {"scenario": scenario.id, "title": scenario.title, "repetition": repetition}
            try:
                outputs = run_scenario(scenario, settings, provider_factory)
            except Exception as exc:  # error del arnés, no del pipeline evaluado
                row["error"] = f"{type(exc).__name__}: {exc}"
                rows.append(row)
                continue
            reference = scenario.expectations()
            feedback = deterministic_checks(outputs, reference, prices)
            if judge is not None and (verdict := judge(outputs, reference)):
                feedback.append(verdict)
            row.update(outputs=outputs, feedback=feedback)
            rows.append(row)
    return rows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals", description="Batería de evaluación de Batcomputer.")
    parser.add_argument("--scenarios", help="IDs separados por comas (por defecto, todos).")
    parser.add_argument("--repeat", type=int, default=1, help="Repeticiones por escenario (default 1).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Proveedor simulado: verifica la instalación sin llamar a ningún modelo.")
    parser.add_argument("--langsmith", action="store_true", help="Sube dataset y experimento a LangSmith.")
    parser.add_argument("--experiment", help="Prefijo del experimento en LangSmith.")
    parser.add_argument("--no-judge", action="store_true", help="Omite el juez LLM.")
    parser.add_argument("--out", default="evals/results", help="Carpeta de resultados (default evals/results).")
    parser.add_argument("--scenario-file", default=str(DEFAULT_PATH))
    parser.add_argument("--list", action="store_true", help="Lista los escenarios y termina.")
    args = parser.parse_args(argv)

    only = [item.strip() for item in args.scenarios.split(",") if item.strip()] if args.scenarios else None
    all_scenarios = load_scenarios(Path(args.scenario_file))
    scenarios = load_scenarios(Path(args.scenario_file), only=only)
    if args.list:
        for scenario in scenarios:
            print(f"{scenario.id:28} {scenario.title}")
        return 0
    if args.repeat < 1:
        parser.error("--repeat debe ser 1 o más")

    settings = Settings()
    if args.dry_run:
        # Sin E2B ni Tavily: el modo simulado no debe generar costos externos.
        settings = settings.model_copy(update={"e2b_api_key": "", "tavily_api_key": ""})
        provider_factory = DryRunProvider
    else:
        provider_factory = make_provider
        if settings.model_backend != "langchain":
            print("Aviso: MODEL_BACKEND=http no reporta tokens ni costo. Use MODEL_BACKEND=langchain.", file=sys.stderr)

    judge = None
    if not (args.dry_run or args.no_judge):
        if not settings.judge_model.strip():
            print("Aviso: JUDGE_MODEL vacío; se omite el juez LLM.", file=sys.stderr)
        else:
            try:
                judge = Judge(settings)
            except (AgentFailure, ValueError) as exc:
                print(f"No se pudo configurar el juez: {exc}", file=sys.stderr)
                return 2

    prices = load_prices()
    models = {"dry-run": "todos"} if args.dry_run else describe_models(settings)
    header = {"fecha": datetime.now().isoformat(timespec="seconds"),
              "modo": "simulado (los puntajes no significan nada)" if args.dry_run else "real",
              "backend": "simulado" if args.dry_run else settings.model_backend, "modelos": ", ".join(f"{k}={v}" for k, v in models.items()),
              "juez": judge.label if judge else "ninguno", "repeticiones": args.repeat,
              "E2B": "sí" if settings.e2b_api_key else "no"}

    if args.langsmith:
        if not configure_tracing(settings.model_copy(update={"langsmith_tracing": True})):
            print("Falta LANGSMITH_API_KEY para usar --langsmith.", file=sys.stderr)
            return 2
        coder = models.get("coder", "dry-run")
        prefix = args.experiment or re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{settings.model_backend}-{coder}")
        outcome = run_langsmith(all_scenarios, scenarios, settings, provider_factory, judge, prices,
                                     args.repeat, prefix, header)
        rows = outcome["rows"]
        header["experimento LangSmith"] = outcome["experiment"]
        header["dataset"] = f"{outcome['dataset']} ({outcome['created']} creados, {outcome['updated']} actualizados)"
    else:
        rows = run_local(scenarios, settings, provider_factory, judge, prices, args.repeat)

    if judge is not None:
        header["tokens del juez (ent/sal)"] = f"{judge.usage['input_tokens']}/{judge.usage['output_tokens']}"
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + ("-simulado" if args.dry_run else "")
    markdown = summarize(rows, header)
    (out_dir / f"{stamp}.json").write_text(json.dumps({"header": header, "rows": rows}, ensure_ascii=False, indent=2,
                                                      default=str), encoding="utf-8")
    (out_dir / f"{stamp}.md").write_text(markdown, encoding="utf-8")
    print(markdown)
    print(f"\nResultados: {out_dir / (stamp + '.md')} y .json", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
