# Rediseño multiagente: código paso a paso

Esta guía muestra cómo se ensambló el nuevo flujo sobre el repositorio existente. El frontend y FastAPI siguen en el mismo servicio; `cartografunk_web` no participa.

## 1. Servidor y sesiones

`app/main.py` crea FastAPI, sirve `app/static/index.html`, expone `/api` y ejecuta un worker en segundo plano. `app/db.py` define `Conversation`, `Message`, `Run` y las tablas de trazas/archivos con SQLAlchemy. En local, `DATABASE_URL=sqlite:///./local.db` crea las tablas al iniciar; en Railway se conserva PostgreSQL de Supabase.

```python
if settings.database_url.startswith("sqlite"):
    Base.metadata.create_all(engine)
```

## 2. Estado tipado y checkpoints

`app/state.py` define el contrato Pydantic `WorkflowState` y las salidas estrictas de Router, Planner, Question, Coder y Reviewer. Un plan de código exige al menos un criterio con descripción y verificación; una aclaración exige una pregunta. Antes de llamar al grafo se valida el estado inicial.

```python
initial = WorkflowState.model_validate(payload).model_dump()
result = graph.invoke(initial, {"configurable": {"thread_id": run_id}})
```

`app/service.py` abre `SqliteSaver` en `LANGGRAPH_SQLITE_PATH`. El identificador del run separa los checkpoints de cada ejecución. El archivo debe residir en un volumen persistente de Railway y el servicio debe tener una sola réplica mientras use SQLite.

```python
with SqliteSaver.from_conn_string(settings.langgraph_sqlite_path) as saver:
    graph = build_graph(provider, settings, record, validator, checkpointer=saver)
```

## 3. Máquina de estados

`app/agents.py` construye Router → Planner → Coder → Tester → Reviewer → Documenter. Router envía `QUESTION` al nodo de respuesta. Planner puede terminar en `needs_clarification` o pedir una búsqueda Tavily antes de fijar criterios. Reviewer devuelve al Coder un error de sintaxis exacto, con máximo tres revisiones.

```python
graph = StateGraph(GraphState)
graph.set_entry_point("router")
graph.add_conditional_edges(
    "router",
    lambda state: "question" if state["route"]["kind"] == "QUESTION" else "planner",
    {"question": "question", "planner": "planner"},
)
graph.add_edge("coder", "validator")
graph.add_edge("validator", "reviewer")
```

El código real usa funciones de ruta dentro de `build_graph`; el bloque resume sus aristas principales. Documenter añade `README.md` con criterios y validación. Si se agotan tres revisiones, el estado queda `exhausted`.

## 4. Tester aislado

`app/sandbox.py` usa `ast.parse` para Python y `json.loads` para JSON cuando no hay E2B; nunca ejecuta código generado localmente. Con E2B, ejecuta `compileall`, pruebas Python y `node --check` para JavaScript y scripts inline de HTML en un sandbox sin red ni secretos de la aplicación.

## 5. Trazas y vista previa

`GET /stream/{session_id}?run_id={run_id}` emite SSE autenticado conforme cada nodo deja una traza. `app/static/index.html` lee el stream con `fetch`, muestra las trazas en un panel lateral y reserva el chat para la respuesta final. Los archivos se pueden elegir en un selector. HTML/JS aprobado se muestra en un `iframe sandbox="allow-scripts"` con CSP restrictiva.

## 6. Modelos y reintento

`app/agents.py` llama a un endpoint Chat Completions con OpenAI, Gemini compatible u Ollama según variables. `ROUTER_MODEL_NAME` permite un nano para clasificación sin obligar a Coder y Reviewer a usarlo. Tenacity reintenta timeouts, red, 429 y 5xx con espera exponencial. Un fallo temporal deja el run `paused` y `POST /api/runs/{id}/retry` lo reencola sin perder el historial ni los checkpoints.

## Verificación

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Las pruebas usan proveedores simulados. Todavía se requiere una prueba con claves reales de modelo, Tavily y E2B antes de afirmar que un ticket o juego generado funciona de punta a punta.
