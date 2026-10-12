"""Resumen en Markdown de una corrida de la batería."""

from statistics import mean


def _get(feedback: list[dict], key: str) -> dict:
    return next((item for item in feedback if item["key"] == key), {})


def _flag(feedback: list[dict], key: str) -> str:
    item = _get(feedback, key)
    if "score" not in item:
        return "-"
    return "sí" if item["score"] == 1 else "no"


def summarize(rows: list[dict], header: dict) -> str:
    lines = ["# Resultados de la batería", ""]
    for key, value in header.items():
        lines.append(f"- **{key}:** {value}")
    lines += ["", "| Escenario | Rep. | Estado | Esperado | Ruta | Trabajo previo | Intentos | Validación | Juez | Tokens (ent/sal) | Costo USD | Seg. |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for row in rows:
        if row.get("error"):
            lines.append(f"| {row['scenario']} | {row['repetition']} | error del arnés | no | - | - | - | - | - | - | - | - |")
            continue
        fb, final = row["feedback"], row["outputs"]["final"]
        judge = _get(fb, "juez_llm").get("score", "-")
        tokens_in, tokens_out = _get(fb, "tokens_entrada").get("score"), _get(fb, "tokens_salida").get("score")
        tokens = f"{tokens_in}/{tokens_out}" if tokens_in is not None else "-"
        cost = _get(fb, "costo_usd")
        cost_text = f"{cost['score']:.4f}" if "score" in cost else cost.get("value", "-")
        lines.append(f"| {row['scenario']} | {row['repetition']} | {final['status']} | {_flag(fb, 'estado_esperado')} | "
                     f"{final['route'] or '-'} | {_flag(fb, 'usa_trabajo_previo')} | {final['attempts'] or '-'} | "
                     f"{final['validation_status'] if final['coder_inputs'] else '-'} | {judge} | {tokens} | {cost_text} | "
                     f"{_get(fb, 'duracion_s').get('score', '-')} |")

    ok_rows = [row for row in rows if not row.get("error")]
    status_ok = [_get(row["feedback"], "estado_esperado").get("score", 0) for row in ok_rows]
    judge_scores = [_get(row["feedback"], "juez_llm")["score"] for row in ok_rows if "score" in _get(row["feedback"], "juez_llm")]
    costs = [_get(row["feedback"], "costo_usd")["score"] for row in ok_rows if "score" in _get(row["feedback"], "costo_usd")]
    lines += ["", "## Agregados", "",
              f"- Estado esperado: {sum(status_ok)}/{len(rows)}",
              f"- Juez LLM promedio: {mean(judge_scores):.2f} ({len(judge_scores)} escenarios)" if judge_scores else "- Juez LLM: no ejecutado",
              f"- Costo estimado total: {sum(costs):.4f} USD" if costs else "- Costo: sin precios o sin conteo de tokens (use MODEL_BACKEND=langchain y evals/prices.toml)",
              f"- Duración total: {sum(_get(row['feedback'], 'duracion_s').get('score', 0) for row in ok_rows):.1f} s",
              "", "## Detalle", ""]
    for row in rows:
        lines.append(f"### {row['scenario']} (repetición {row['repetition']}): {row.get('title', '')}")
        if row.get("error"):
            lines += ["", f"Error del arnés: {row['error']}", ""]
            continue
        final = row["outputs"]["final"]
        lines.append("")
        for item in row["feedback"]:
            if item.get("comment"):
                lines.append(f"- {item['key']}: {item['comment']}")
        for error in final["errors"]:
            lines.append(f"- error en {error['stage']}: {error['error']}")
        if final["reviews"] and not final["reviews"][-1]["approved"]:
            lines.append("- último feedback del Reviewer: " + " | ".join(final["reviews"][-1]["feedback"][:3]))
        lines.append(f"- respuesta final: {final['result'][:300]}")
        lines.append("")
    return "\n".join(lines)
