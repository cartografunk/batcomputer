"""Carga y valida los escenarios de evals/scenarios.toml."""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PATH = Path(__file__).with_name("scenarios.toml")
STATUSES = {"approved", "answered", "needs_clarification", "exhausted", "technical_error", "paused"}
ROUTES = {"NEW_TICKET", "MODIFICATION", "QUESTION"}
FAILURES = {"auth", "timeout"}


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    turns: list[str]
    expected_status: list[str]
    rubric: str = ""
    expected_route: str | None = None
    expect_files: bool | None = None
    file_patterns: list[str] = field(default_factory=list)
    check_previous_work: bool = False
    expected_scout: bool | None = None
    seed: str | None = None
    failure: str | None = None
    judge: bool = True

    def expectations(self) -> dict:
        """Salidas de referencia (reference_outputs en LangSmith)."""
        data = {"expected_status": list(self.expected_status), "rubric": self.rubric.strip(),
                "file_patterns": list(self.file_patterns), "check_previous_work": self.check_previous_work,
                "judge": self.judge}
        for key in ("expected_route", "expect_files", "expected_scout", "seed", "failure"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        return data


def load_scenarios(path: Path = DEFAULT_PATH, only: list[str] | None = None) -> list[Scenario]:
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8")).get("scenario", [])
    scenarios = [Scenario(**{k: (list(v) if isinstance(v, list) else v) for k, v in item.items()}) for item in raw]
    ids = [scenario.id for scenario in scenarios]
    if len(ids) != len(set(ids)):
        raise ValueError("Hay ids de escenario duplicados en scenarios.toml.")
    for scenario in scenarios:
        problems = []
        if not scenario.turns:
            problems.append("sin turnos")
        if not set(scenario.expected_status) <= STATUSES or not scenario.expected_status:
            problems.append(f"expected_status debe estar en {sorted(STATUSES)}")
        if scenario.expected_route and scenario.expected_route not in ROUTES:
            problems.append(f"expected_route debe estar en {sorted(ROUTES)}")
        if scenario.failure and scenario.failure not in FAILURES:
            problems.append(f"failure debe estar en {sorted(FAILURES)}")
        if scenario.judge and not scenario.rubric.strip():
            problems.append("judge=true requiere rubric")
        if problems:
            raise ValueError(f"Escenario {scenario.id}: " + "; ".join(problems))
    if only:
        missing = set(only) - set(ids)
        if missing:
            raise ValueError(f"Escenarios inexistentes: {', '.join(sorted(missing))}")
        scenarios = [scenario for scenario in scenarios if scenario.id in only]
    return scenarios
