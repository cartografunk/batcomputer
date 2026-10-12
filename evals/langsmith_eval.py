"""Sincroniza la batería como dataset de LangSmith y la corre como experimento.

Cada escenario es un ejemplo con inputs {scenario_id, turns} y reference_outputs = expectativas.
El experimento guarda, por ejemplo, la traza completa (grafo, agentes, llamadas al modelo) y el
feedback de las métricas; así se comparan modelos o prompts en la vista de experimentos.
"""

from .runner import run_scenario
from .scoring import deterministic_checks

DATASET_NAME = "batcomputer-bateria"


def sync_dataset(client, scenarios, name: str = DATASET_NAME):
    """Crea el dataset si no existe y crea o actualiza un ejemplo por escenario (clave: scenario_id)."""
    if client.has_dataset(dataset_name=name):
        dataset = client.read_dataset(dataset_name=name)
    else:
        dataset = client.create_dataset(name, description="Batería de evaluación de Batcomputer (evals/scenarios.toml).")
    existing = {(example.inputs or {}).get("scenario_id"): example for example in client.list_examples(dataset_id=dataset.id)}
    new, updated = [], 0
    for scenario in scenarios:
        inputs = {"scenario_id": scenario.id, "turns": list(scenario.turns)}
        outputs = scenario.expectations()
        metadata = {"title": scenario.title}
        example = existing.get(scenario.id)
        if example is None:
            new.append({"inputs": inputs, "outputs": outputs, "metadata": metadata})
        elif (example.inputs != inputs or (example.outputs or {}) != outputs
              or (example.metadata or {}).get("title") != scenario.title):
            client.update_example(example.id, inputs=inputs, outputs=outputs, metadata=metadata)
            updated += 1
    if new:
        client.create_examples(dataset_id=dataset.id, examples=new)
    return dataset, len(new), updated


def run_langsmith(all_scenarios, selected, settings, provider_factory, judge, prices, repetitions,
                  experiment_prefix, metadata, client=None):
    if client is None:
        from langsmith import Client
        client = Client()
    dataset, created, updated = sync_dataset(client, all_scenarios)
    by_id = {scenario.id: scenario for scenario in selected}
    examples = [example for example in client.list_examples(dataset_id=dataset.id)
                if (example.inputs or {}).get("scenario_id") in by_id]

    def target(inputs: dict) -> dict:
        return run_scenario(by_id[inputs["scenario_id"]], settings, provider_factory)

    def batcomputer_checks(outputs: dict, reference_outputs: dict) -> list[dict]:
        return deterministic_checks(outputs, reference_outputs, prices)

    evaluators = [batcomputer_checks]
    if judge is not None:
        def juez_llm(outputs: dict, reference_outputs: dict) -> dict:
            return judge(outputs, reference_outputs) or {"key": "juez_llm", "comment": "No aplica a este escenario."}
        evaluators.append(juez_llm)

    results = client.evaluate(target, data=examples, evaluators=evaluators, experiment_prefix=experiment_prefix,
                              num_repetitions=repetitions, max_concurrency=1, metadata=metadata,
                              description="Batería de evaluación de Batcomputer")
    rows, seen = [], {}
    for row in results:
        scenario_id = (row["example"].inputs or {}).get("scenario_id")
        seen[scenario_id] = seen.get(scenario_id, 0) + 1
        feedback = [{"key": item.key, "score": item.score, "value": item.value, "comment": item.comment or ""}
                    for item in row["evaluation_results"]["results"]]
        feedback = [{k: v for k, v in item.items() if v is not None} for item in feedback]
        outputs = row["run"].outputs
        entry = {"scenario": scenario_id, "title": by_id[scenario_id].title, "repetition": seen[scenario_id]}
        if not outputs or "final" not in outputs:
            entry["error"] = row["run"].error or "sin salidas"
        else:
            entry.update(outputs=outputs, feedback=feedback)
        rows.append(entry)
    return {"experiment": results.experiment_name, "dataset": dataset.name, "created": created,
            "updated": updated, "rows": rows}
