# Contrato HTTP de Batcomputer para la interfaz

`GET /api/session` crea o recupera una sesión anónima mediante cookie `HttpOnly`, `SameSite=Lax` y `Secure` en producción. El frontend usa esa cookie automáticamente para `/api` y `/stream`; no solicita token. Los clientes anteriores pueden seguir usando `Authorization: Bearer <token>` con una entrada de `AUTH_TOKENS`. Los recursos de otra sesión o token devuelven 404. JSON UTF-8. `401` indica sesión ausente o credencial inválida, `429` indica límite de uso, `422` indica entrada inválida. Los UUID identifican recursos pero no autorizan acceso.

| Método | Ruta | Respuesta |
|---|---|---|
| POST | `/api/conversations` | `201 {id,created_at}` |
| GET | `/api/conversations/{id}` | `{id,created_at,pending_clarification,messages:[{id,role,content,created_at}]}` |
| POST | `/api/conversations/{id}/messages` | Enviar `{content}`; `202 {run_id,status:"queued"}` |
| GET | `/api/runs/{id}` | `{id,conversation_id,status,result,attempts,api_retries,validation_status,recovery_count,error,created_at,updated_at}` |
| POST | `/api/runs/{id}/retry` | `202 {run_id,status:"queued"}` si el run está `paused`; `409` en otro estado |
| GET | `/api/runs/{id}/traces` | Array de `{stage,attempt,duration_ms,input,output,error,created_at}` |
| GET | `/stream/{session_id}?run_id={id}` | SSE autenticado con eventos `status` y `trace`; cada traza contiene `{node,status,attempt,duration_ms}` |
| GET | `/api/runs/{id}/files` | Array de `{attempt,path,content}`; incluye versiones rechazadas |
| GET | `/health` | `{status:"ok"}` si PostgreSQL responde; `503` en caso contrario. No requiere token. |

Estados: `queued` (pendiente), `running` (en proceso), `answered` (pregunta contestada sin implementación), `needs_clarification` (falta una decisión del usuario), `approved` (Reviewer aprobó), `exhausted` (rechazo tras `MAX_CODER_ATTEMPTS`), `paused` (proveedor temporalmente indisponible; reintentable) y `technical_error` (otro fallo técnico o interrupciones repetidas). Los últimos seis terminan la ejecución actual; `paused` puede reencolarse y después de `needs_clarification` se puede enviar otro mensaje. `result` contiene solo la respuesta final para el chat. `attempts` cuenta invocaciones de Coder; `api_retries` cuenta reintentos de transporte del proveedor aparte.

`validation_status` es independiente de `status`: `not_executed` (sin `E2B_API_KEY` o run sin código), `passed`, `failed` (compilación o pruebas fallaron) o `error` (infraestructura del sandbox falló). Un `approved` puede coexistir con `error`: ese resultado significa aprobación estática sin validación completa. La traza `validator` incluye rutas, salida acotada y duración por intento. `recovery_count` indica cuántas veces se recuperó un run interrumpido. Los trabajos se recuperan hasta `MAX_JOB_RECOVERIES`; no hay garantía de exactamente una ejecución externa.

El frontend abre `/stream/{session_id}` con `fetch` y `Authorization` para ver cada transición; consulta `/api/runs/{id}` hasta un estado terminal para la respuesta final. Cada traza lleva un identificador, aunque el cliente actual no reconecta automáticamente el stream. En una pregunta o aclaración, `/files` queda vacío. En una entrega, Documenter añade un `README.md`. Para obtener el estado del conjunto de archivos del hilo, recupere las versiones aprobadas a través de los runs anteriores; la API de archivos por run conserva todas las propuestas, incluidas las rechazadas. Una vista previa de HTML/JS solo debe renderizarse en un iframe `sandbox="allow-scripts"` sin `allow-same-origin`, con CSP que bloquee conexiones y recursos externos. No navegue directamente a contenido generado bajo el origen de la aplicación. El build del diseñador se sirve desde `app/static`; las rutas `/api`, `/health`, `/stream` y `/flappy` están reservadas.
