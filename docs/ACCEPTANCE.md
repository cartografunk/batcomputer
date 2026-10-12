# Criterios de aceptación de la prueba técnica

Este documento resume con palabras propias el enunciado facilitado para la prueba. El archivo original de la empresa no se incluye en el repositorio: contiene datos de contacto, elementos de maquetación y una nota editorial sobre la ubicación de Flappy Bird. No es necesario para instalar ni ejecutar Batcomputer.

| Criterio | Evidencia en Batcomputer | Estado |
|---|---|---|
| Ticket libre convertido en código mediante Router, Planner, Coder, Tester, Reviewer y Documenter | Grafo en `app/agents.py`, contratos en `app/state.py`; proveedor configurable | Implementado; falta probar calidad con un modelo real |
| Rechazo con feedback, corrección y límite explícito | `MAX_CODER_ATTEMPTS=3`, estado `exhausted`, versiones y revisiones persistidas | Verificado con proveedor simulado |
| Chat de varios turnos y cambios sobre archivos previos | Conversaciones, mensajes, archivos aprobados y aclaraciones pendientes en base de datos | Verificado con proveedor simulado |
| Una respuesta final por turno y trazas aparte en vivo | Chat, `/stream/{session_id}` por SSE y endpoints de trazas y archivos | Verificado localmente |
| Timeouts, JSON malformado y errores comprensibles | Tenacity, checkpoint SQLite, estados `paused` y `technical_error` | Verificado con proveedor simulado |
| Clave del modelo fuera del repositorio | `MODEL_API_KEY` únicamente en variables del servidor | Implementado; revisar variables de Railway antes del despliegue |
| Aplicación pública de una sola página y disponible el día de la entrevista | [batcomputer.cartografunk.com](https://batcomputer.cartografunk.com/), Dockerfile, `/health`, frontend servido por FastAPI | Desplegado; volver a comprobar disponibilidad el día de la entrevista |
| Repositorio con instalación, variables, arquitectura y resumen | `README.md`, `docs/API.md`, `docs/DECISIONS.md` | Implementado |
| Flappy Bird jugable | Página independiente `/flappy`, implementada en el repositorio; el chat acepta entregas HTML5 y las muestra en iframe restringido | Entregable independiente implementado; generación por agentes pendiente de comprobación real con `S7-flappy` |

## Prueba manual antes de la entrevista

1. Con `MODEL_API_KEY` y, si se usará validación Python, `E2B_API_KEY` configuradas en Railway, enviar el ejemplo de cupones del enunciado. Revisar que el código no esté predefinido, que Planner planifique y que Reviewer emita un veredicto. No presentar las pruebas con proveedor simulado como validación de esta ejecución real.
2. En el mismo hilo, solicitar un cambio y comprobar que Coder recibe los archivos aprobados anteriores. Después preguntar por lo entregado y comprobar que el sistema puede contestar sin generar nuevos archivos.
3. Probar otro ticket de complejidad similar no usado en el desarrollo. Revisar la respuesta final, archivos, validación, trazas y comportamiento ante rechazo o error.
4. Confirmar que la URL pública, el chat y `/health` responden durante la entrevista. Probar Flappy Bird y el enlace de regreso a Cartografunk.

La URL del sistema está publicada. La disponibilidad debe comprobarse de nuevo el día de la entrevista.

## Verificación pública del 12 de octubre de 2026

- `/health` y `/flappy` respondieron HTTP 200. Flappy Bird se inició en el navegador.
- Un ticket de cupones con contrato y casos explícitos terminó `approved` en un intento y entregó `coupons.py`, pruebas y README. Las trazas incluyeron Router, Planner, Coder, Validator, Reviewer y Documenter.
- En otro hilo, una calculadora terminó `approved`; la petición de agregar multiplicación también terminó `approved`. El segundo run tomó el snapshot anterior como base y su archivo final conservó `add` y `subtract` junto con `multiply`. Una pregunta posterior terminó `answered` sin archivos nuevos.
- La validación de esas entregas fue `not_executed`: no hay `E2B_API_KEY` configurada. La aprobación del Reviewer no demuestra que las pruebas generadas se hayan ejecutado. El ticket inicial de cupones, menos específico, pidió aclaración; conviene mostrar también ese comportamiento durante la entrevista.
- Tras cambiar Coder a salida estructurada estricta en Azure, un pedido real de Flappy Bird para navegador terminó `approved` y produjo `index.html` y README. El script inline pasó `node --check`; el HTML generado aún no se probó jugando. `/flappy` sigue siendo una implementación independiente.

Queda pendiente ejecutar y revisar el código generado en un sandbox real, repetir el ejemplo exacto del enunciado y confirmar de nuevo la disponibilidad el día de la entrevista.
