# Contrato HTTP de Batcomputer para la interfaz

Todos los endpoints `/api` requieren `Authorization: Bearer <token>`. El token corresponde a una entrada de `AUTH_TOKENS`; los recursos de otro token devuelven 404. JSON UTF-8. `401` indica token ausente o inválido, `429` indica límite por hora, `422` indica entrada inválida. Los UUID identifican recursos pero no autorizan acceso.

| Método | Ruta | Respuesta |
|---|---|---|
| POST | `/api/conversations` | `201 {id,created_at}` |
| GET | `/api/conversations/{id}` | `{id,created_at,pending_clarification,messages:[{id,role,content,created_at}]}` |
| POST | `/api/conversations/{id}/messages` | Enviar `{content}`; `202 {run_id,status:"queued"}` |
| GET | `/api/runs/{id}` | `{id,conversation_id,status,result,attempts,api_retries,validation_status,recovery_count,error,created_at,updated_at}` |
| GET | `/api/runs/{id}/traces` | Array de `{stage,attempt,duration_ms,input,output,error,created_at}` |
| GET | `/api/runs/{id}/files` | Array de `{attempt,path,content}`; incluye versiones rechazadas |
| GET | `/health` | `{status:"ok"}` si PostgreSQL responde; `503` en caso contrario. No requiere token. |

Estados: `queued` (pendiente), `running` (en proceso), `answered` (pregunta contestada sin implementación), `needs_clarification` (falta una decisión del usuario), `approved` (Reviewer aprobó), `exhausted` (rechazo tras `MAX_CODER_ATTEMPTS`) y `technical_error` (fallo técnico o trabajo interrumpido demasiadas veces). Los cinco últimos son terminales para ese run; después de `needs_clarification`, el usuario puede enviar otro mensaje al mismo hilo. `result` contiene solo la respuesta final para el chat. `attempts` cuenta invocaciones de Coder, incluida una respuesta malformada; `api_retries` cuenta reintentos de transporte del proveedor aparte.

`validation_status` es independiente de `status`: `not_executed` (sin `E2B_API_KEY` o run sin código), `passed`, `failed` (compilación o pruebas fallaron) o `error` (infraestructura del sandbox falló). Un `approved` puede coexistir con `error`: ese resultado significa aprobación estática sin validación completa. La traza `validator` incluye rutas, salida acotada y duración por intento. `recovery_count` indica cuántas veces se recuperó un run interrumpido. Los trabajos se recuperan hasta `MAX_JOB_RECOVERIES`; no hay garantía de exactamente una ejecución externa.

El frontend debe consultar el estado con intervalos hasta un estado terminal. Muestre `result` en el chat y trazas y archivos en un panel separado. En una pregunta o aclaración, `/files` queda vacío. Para obtener el estado del conjunto de archivos del hilo, recupere las versiones aprobadas a través de los runs anteriores; la API de archivos por run conserva todas las propuestas, incluidas las rechazadas. Una vista previa de HTML/JS solo debe renderizarse en un iframe `sandbox="allow-scripts"` sin `allow-same-origin`, con CSP que bloquee conexiones y recursos externos. No navegue directamente a contenido generado bajo el origen de la aplicación. El build del diseñador se sirve desde `app/static`; las rutas `/api`, `/health` y `/flappy` están reservadas.
