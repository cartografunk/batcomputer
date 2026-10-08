# Evaluación y trazas

Dos piezas independientes:

1. **Backend LangChain** (`MODEL_BACKEND=langchain`, archivo `app/lc_provider.py`): mismo contrato que el cliente HTTP original, pero cada agente queda como una traza hija del grafo en LangSmith, se cuentan tokens por modelo y cada rol puede usar un modelo distinto.
2. **Batería de evaluación** (`python -m evals`, carpeta `evals/`): 10 escenarios que se corren con el modelo real y se califican con verificaciones deterministas y un juez LLM opcional. Sirve para demostrar con evidencia que el pipeline funciona y para comparar modelos o prompts.

El backend `http` sigue siendo el predeterminado; nada cambia hasta configurar las variables nuevas.

## Configuración mínima con una clave de OpenAI

En `.env` (local) o en Variables de Railway:

```
MODEL_BACKEND=langchain
MODEL_API_KEY=<clave de OpenAI>
MODEL_BASE_URL=https://api.openai.com/v1
MODEL_NAME=<modelo principal>
ROUTER_MODEL_NAME=<modelo ligero opcional>
JUDGE_MODEL=openai:<modelo para el juez>
```

Opcional:

| Variable | Uso |
|---|---|
| `ROLE_MODELS` | Modelo por rol: `router=openai:<mini>,coder=openai:<principal>,reviewer=anthropic:claude-sonnet-5-5`. Roles: router, question, planner, coder, reviewer. Proveedores: remote (usa `MODEL_BASE_URL`), openai, anthropic, openrouter, google, azure, azure_responses, ollama |
| `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, `OPENAI_API_KEY` | Claves de los proveedores usados en `ROLE_MODELS` o `JUDGE_MODEL`. `openai:` usa `OPENAI_API_KEY` y, si está vacía, `MODEL_API_KEY` |
| `LANGSMITH_TRACING=true`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT` | Envía las trazas de la app y de la batería a LangSmith. `LANGSMITH_ENDPOINT` solo para la región EU |
| `E2B_API_KEY` | Ejecuta el código generado en sandbox; sin ella la métrica `validacion_ejecutada` queda `not_executed` |
| `MAX_OUTPUT_TOKENS` | Tope de salida para Claude (default 16384) |

Recomendación: Reviewer y juez de una familia distinta a la del Coder, para reducir el riesgo de aprobar por afinidad. Con solo la clave de OpenAI la batería funciona igual; anótelo como limitación del resultado.

Notas:

- Producción sigue exigiendo `MODEL_API_KEY` al arrancar aunque todos los roles usen otro proveedor (validación existente en `app/main.py`).
- Claude se usa con salida estructurada nativa (`output_config.format`); OpenRouter con `json_schema` y `require_parameters`; OpenAI, Azure, Gemini y Ollama con modo JSON, igual que el cliente original.
- No se usa el paquete `langchain` (`init_chat_model`) porque exige LangGraph 1.x y el repo fija 0.6.

## Comandos

```powershell
# 1. Verificar la instalación sin llamar a ningún modelo (puntajes sin significado)
.\.venv\Scripts\python.exe -m evals --dry-run

# 2. Listar escenarios
.\.venv\Scripts\python.exe -m evals --list

# 3. Correr con el modelo real (todos o algunos escenarios)
.\.venv\Scripts\python.exe -m evals
.\.venv\Scripts\python.exe -m evals --scenarios S1-cupones,S2-seguimiento --repeat 3

# 4. Subir dataset y experimento a LangSmith
.\.venv\Scripts\python.exe -m evals --langsmith --experiment openai-base
```

Cada corrida deja `evals/results/<fecha>.md` (resumen legible) y `.json` (todo lo recogido, incluido el código entregado). La carpeta está en `.gitignore`.

Cada escenario usa una base SQLite y checkpoints temporales, el mismo worker que producción (`claim_next` + `process_run`) y el proveedor configurado. No toca la base real ni requiere el servidor web.

## Escenarios (`evals/scenarios.toml`)

| ID | Qué mide | Estado aceptable |
|---|---|---|
| S1-cupones | Ticket del enunciado | approved |
| S2-seguimiento | "Agrégale también..." modifica lo entregado | approved, ruta MODIFICATION, Coder recibe archivos previos |
| S3-pregunta | Pregunta sobre lo entregado sin generar código | answered |
| S4-ambiguo | Pide aclaración o declara supuestos | needs_clarification o approved |
| S5-imposible | No aprueba un requisito imposible | needs_clarification, exhausted o answered |
| S6-tras-agotamiento | "Arréglalo" después de un turno agotado (historial sembrado) | approved y Coder recibe el código rechazado |
| S7-flappy | Flappy Bird jugable desde el chat | approved |
| S8-api-tareas | Ticket de otro dominio | approved |
| S9a-fallo-autenticacion | Clave inválida: error claro | technical_error |
| S9b-fallo-timeout | Timeout: estado recuperable | paused |

Para agregar un escenario, copie un bloque `[[scenario]]`, use un `id` nuevo y escriba una rúbrica con criterios observables. No cambie `id` existentes: enlazan los resultados en LangSmith.

## Métricas

| Clave | Tipo | Significado |
|---|---|---|
| `estado_esperado` | 0/1 | Estado final dentro de los aceptables |
| `ruta_esperada` | 0/1 | Clasificación del Router |
| `entrega_codigo` / `sin_codigo_nuevo` | 0/1 | Hay (o no hay) archivos de código en la entrega final |
| `patrones_en_codigo` | 0-1 | Fracción de expresiones regulares encontradas en el código |
| `usa_trabajo_previo` | 0/1 | El Coder recibió archivos del turno anterior |
| `intentos`, `aprobado_primer_intento` | número, 0/1 | Invocaciones del Coder |
| `validacion_ejecutada` | 0/1 o texto | `passed`/`failed` en E2B; si no se ejecutó, el estado como texto |
| `mensaje_de_error_claro` | 0/1 | En fallos inducidos: mensaje no vacío y sin detalles internos |
| `tokens_entrada`, `tokens_salida`, `costo_usd` | número | Solo con `MODEL_BACKEND=langchain`; costo con `evals/prices.toml` |
| `duracion_s` | número | Tiempo total del escenario |
| `juez_llm` | 0-1 | Calificación del juez contra la rúbrica, con justificación |

## Qué esperar en la primera corrida

- **S6 `usa_trabajo_previo = 0`** con el código actual: el siguiente turno solo recibe entregas aprobadas, así que el código rechazado no llega al Coder (defecto D1 del informe de revisión). La métrica queda en 1 cuando se corrija.
- **`validacion_ejecutada = not_executed`** salvo con E2B y pruebas generadas por el Coder; su prompt no las pide todavía.
- El juez es un modelo: use `--repeat 3` antes de comparar configuraciones y revise las justificaciones, no solo el número.

## Costo aproximado

Una corrida completa son 12 turnos (S2 y S3 tienen dos): 2 llamadas al modelo en una pregunta y entre 4 y 8 en un ticket, según los rechazos, más una llamada del juez por escenario. Los fallos inducidos no consumen tokens. Con modelos tipo mini suele costar centavos; con modelos grandes, unos pocos dólares. El resumen reporta el costo exacto cuando los modelos usados tienen precio en `evals/prices.toml`.

## Privacidad

Con `LANGSMITH_TRACING=true` se envían a LangSmith los tickets, el historial y el código generado; nunca las claves. No active trazas en producción con datos sensibles sin revisar retención y región.
