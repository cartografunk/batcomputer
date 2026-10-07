# Contrato HTTP para la interfaz

Todos los endpoints `/api` requieren `Authorization: Bearer <token>`. El token corresponde a una entrada de `AUTH_TOKENS`; los recursos de otro token devuelven 404. JSON UTF-8. `401` indica token ausente o inválido, `429` indica límite por hora, `422` indica entrada inválida. Los UUID identifican recursos pero no autorizan acceso.

| Método | Ruta | Respuesta |
|---|---|---|
| POST | `/api/conversations` | `201 {id,created_at}` |
| GET | `/api/conversations/{id}` | `{id,created_at,messages:[{id,role,content,created_at}]}` |
| POST | `/api/conversations/{id}/messages` | Enviar `{content}`; `202 {run_id,status:"queued"}` |
| GET | `/api/runs/{id}` | `{id,conversation_id,status,result,attempts,api_retries,execution,error,created_at,updated_at}` |
| GET | `/api/runs/{id}/traces` | Array de `{stage,attempt,duration_ms,input,output,error,created_at}` |
| GET | `/api/runs/{id}/files` | Array de `{attempt,path,content}`; incluye versiones rechazadas |

Estados: `queued` (pendiente), `running` (en proceso), `answered` (pregunta contestada sin implementación), `approved` (Reviewer aprobó), `exhausted` (rechazo tras `MAX_CODER_ATTEMPTS`), `technical_error` (modelo, validación o ejecución interna falló). Los cuatro últimos son terminales. `result` contiene solo la respuesta final para el chat. `execution` vale `not_executed` incluso cuando `status=approved`: el Reviewer hizo revisión estática. `attempts` cuenta invocaciones de Coder; `api_retries` cuenta reintentos de transporte del proveedor aparte.

El frontend debe consultar el estado con intervalos hasta un estado terminal. Muestre `result` en el chat y trazas y archivos en un panel separado. En una pregunta, `/files` queda vacío. Para obtener el estado del conjunto de archivos del hilo, recupere las versiones aprobadas a través de los runs anteriores o use la vista de conversación para identificar sus mensajes; la API de archivos por run conserva el historial completo por intento.
