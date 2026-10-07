# Criterios de aceptación de la prueba técnica

Este documento resume con palabras propias el enunciado facilitado para la prueba. El archivo original de la empresa no se incluye en el repositorio: contiene datos de contacto, elementos de maquetación y una nota editorial sobre la ubicación de Flappy Bird. No es necesario para instalar ni ejecutar Batcomputer.

| Criterio | Evidencia en Batcomputer | Estado |
|---|---|---|
| Ticket libre convertido en código mediante Planner, Coder y Reviewer | Grafo en `app/agents.py`; proveedor configurable | Implementado; falta probar calidad con un modelo real |
| Rechazo con feedback, corrección y límite explícito | `MAX_CODER_ATTEMPTS=3`, estado `exhausted`, versiones y revisiones persistidas | Verificado con proveedor simulado |
| Chat de varios turnos y cambios sobre archivos previos | Conversaciones, mensajes, archivos aprobados y aclaraciones pendientes en base de datos | Verificado con proveedor simulado |
| Una respuesta final por turno y trazas consultables aparte | Chat mínimo y endpoints `/api/runs/{id}`, `/traces`, `/files` | Verificado localmente |
| Timeouts, JSON malformado y errores comprensibles | Reintentos técnicos separados y estado `technical_error` | Verificado con proveedor simulado |
| Clave del modelo fuera del repositorio | `MODEL_API_KEY` únicamente en variables del servidor | Implementado; revisar variables de Railway antes del despliegue |
| Aplicación pública de una sola página y disponible el día de la entrevista | Dockerfile, `/health`, frontend servido por FastAPI, guía de Railway | **Pendiente:** desplegar, entregar URL y supervisar disponibilidad |
| Repositorio con instalación, variables, arquitectura y resumen | `README.md`, `docs/API.md`, `docs/DECISIONS.md` | Implementado |
| Flappy Bird jugable | Página `/flappy` | Implementado; integración con chat pendiente de confirmación |

## Prueba manual antes de la entrevista

1. Con `MODEL_API_KEY` y, si se usará validación Python, `E2B_API_KEY` configuradas en Railway, enviar el ejemplo de cupones del enunciado. Revisar que el código no esté predefinido, que Planner planifique y que Reviewer emita un veredicto. No presentar las pruebas con proveedor simulado como validación de esta ejecución real.
2. En el mismo hilo, solicitar un cambio y comprobar que Coder recibe los archivos aprobados anteriores. Después preguntar por lo entregado y comprobar que el sistema puede contestar sin generar nuevos archivos.
3. Probar otro ticket de complejidad similar no usado en el desarrollo. Revisar la respuesta final, archivos, validación, trazas y comportamiento ante rechazo o error.
4. Confirmar que la URL pública, el chat y `/health` responden durante la entrevista. Probar Flappy Bird y el enlace de regreso a Cartografunk.

La URL del sistema y su disponibilidad son entregables del enunciado. Permanecen pendientes porque el despliegue no está autorizado todavía.
