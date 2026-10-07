# Batcomputer

Batcomputer es un monolito con pipeline multiagente: convierte tickets en archivos mediante Planner, Coder, validación aislada y Reviewer. Cada turno persiste y muestra una respuesta final; trazas y archivos quedan separados. Este repositorio contiene la aplicación completa y se despliega por separado de la web principal de Cartografunk.

## Instalación local

Requiere Python 3.11 o superior. Basta clonar [cartografunk/batcomputer](https://github.com/cartografunk/batcomputer); no necesita `cartografunk/cartografunk_web`. En PowerShell, desde la raíz de este repositorio:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
Copy-Item .env.example .env
# Edite AUTH_TOKENS y, para usar un modelo real, MODEL_API_KEY.
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Abra `http://localhost:8000`, introduzca un token configurado en `AUTH_TOKENS` y envíe un ticket. `/flappy` es un juego independiente y jugable; su integración con el chat espera confirmación. SQLite sirve solo para desarrollo local y crea tablas automáticamente. Producción usa el esquema `batcomputer` del proyecto Supabase existente. Aplique las tres migraciones de `migrations/` en el orden indicado abajo. Estas protegen las tablas internas con RLS y revocan el acceso de `anon` y `authenticated`; Batcomputer usa PostgreSQL desde el servidor, no la Data API de Supabase.

## Variables

Vea `.env.example` para la lista completa. `DATABASE_URL` usa `sqlite:///./local.db` localmente o `postgresql+psycopg://...?sslmode=require` para Supabase; copie host, usuario y puerto del panel **Connect** y codifique los caracteres especiales de la contraseña en la URL. El backend fija `search_path=batcomputer` en las conexiones PostgreSQL. Use conexión directa o el pooler en modo **Session**; el modo **Transaction** no conserva el estado de sesión necesario. `AUTH_TOKENS` contiene tokens opacos separados por comas, distintos por usuario. `MODEL_API_KEY`, `TAVILY_API_KEY` y `E2B_API_KEY` solo existen en el servidor. `MODEL_BASE_URL` debe admitir Chat Completions y salida JSON; `MODEL_NAME` selecciona el modelo. `MAX_CODER_ATTEMPTS=3` significa implementación inicial y hasta dos correcciones. `MODEL_API_RETRIES` contabiliza aparte reintentos por red, timeout y 429/5xx. `MAX_JOB_RECOVERIES`, `MAX_CONCURRENT_RUNS`, `MAX_QUEUED_RUNS_PER_OWNER`, `RATE_LIMIT_PER_HOUR` y `JOB_STALE_SECONDS` limitan uso y recuperación. `CORS_ORIGINS` solo necesita el origen del módulo si el frontend y la API se sirven juntos.

### Modelo y búsqueda

Para la primera prueba real con la clave de Iram, recomendamos `MODEL_NAME=gpt-6.1-sol` con `MODEL_BASE_URL=https://api.openai.com/v1`: el trabajo central es generar y revisar código, y este modelo admite Chat Completions y salida estructurada. Confirme que la clave tenga acceso al modelo antes de usarlo. El valor de `.env.example` permanece como ejemplo económico de desarrollo. Compare luego calidad, latencia y consumo con tickets representativos; no hay evaluación real del modelo todavía. [Modelo GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol).

Gemini puede usarse de dos formas. Para ejecutar **todo** el pipeline con Gemini API mediante la capa compatible de Google, configure `MODEL_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/`, `MODEL_NAME=gemini-3.8-flash` y `MODEL_API_KEY=<clave de Gemini API>`. Para un modo mixto, mantenga el modelo principal de Iram en `MODEL_*` y configure `GEMINI_API_KEY=<clave de Gemini API>`, `GEMINI_MODEL_NAME=gemini-3.8-flash` y `GEMINI_ROLES=reviewer` (también se aceptan `planner` y `coder`, separados por comas). Una clave ausente en un rol Gemini solicitado produce error explícito. La compatibilidad con el protocolo está documentada por Google, pero la salida JSON de este pipeline aún requiere una prueba real con la clave. La suscripción a la app Gemini no sustituye la clave de API: cree o consulte la clave en [Google AI Studio](https://aistudio.google.com/apikey) y revise cuota/facturación del proyecto. No pegue claves en tickets, chats ni commits. [Compatibilidad Gemini](https://ai.google.dev/gemini-api/docs/openai), [modelos](https://ai.google.dev/gemini-api/docs/models), [claves](https://ai.google.dev/gemini-api/docs/api-key).

`TAVILY_API_KEY` habilita investigación opcional. El Planner pide **una sola búsqueda** cuando necesita documentación o hechos externos actuales; el grafo guarda consulta, fuentes y resultado en trazas, vuelve a planificar y pasa las fuentes al Coder y Reviewer. La búsqueda usa profundidad `basic`, máximo configurable de 1 a 5 resultados (`TAVILY_MAX_RESULTS`, predeterminado 3), timeout propio y extractos truncados. Si Tavily falla, el trabajo sigue sin resultados; el Planner no debe inventar hechos actuales. Las páginas encontradas son datos no confiables, no instrucciones para los agentes. Sin clave, no hay búsquedas ni gasto de Tavily. [API Tavily Search](https://docs.tavily.com/documentation/api-reference/endpoint/search).

## Arquitectura

```mermaid
flowchart LR
  UI[Chat web y Flappy Bird] --> API[FastAPI]
  API --> DB[(Supabase PostgreSQL; SQLite local)]
  DB --> Worker[Worker respaldado por PostgreSQL]
  Worker --> Planner
  Planner -->|pregunta| Answer[Respuesta]
  Planner -->|aclaración| Clarify[Aclaración pendiente]
  Planner -->|requiere información externa y Tavily configurado| Research[Tavily]
  Research --> Planner
  Planner -->|ticket| Coder
  Coder --> Validator[Validador E2B opcional]
  Validator --> Reviewer
  Reviewer -->|rechazo y quedan intentos| Coder
  Reviewer -->|aprobado| Approved[Aprobado]
  Reviewer -->|sin intentos| Exhausted[Agotado]
  Planner -->|error| Failed[Error técnico]
  Coder -->|error| Failed
  Reviewer -->|error| Failed
  Answer --> DB
  Clarify --> DB
  Approved --> DB
  Exhausted --> DB
  Failed --> DB
```

El worker reclama trabajos bajo un bloqueo transaccional de PostgreSQL, limita ejecuciones simultáneas y serializa los cambios de cada conversación. Renueva la señal de vida durante llamadas largas; tras `JOB_STALE_SECONDS` recupera un trabajo hasta `MAX_JOB_RECOVERIES` veces. Un lease impide que un worker antiguo persista resultados. Es una cola de entrega al menos una vez, con control de duplicados, no una promesa de ejecución exactamente una vez. SQLite se usa con un único proceso local. Los archivos aprobados previos forman el contexto de cambios posteriores; las propuestas rechazadas y su feedback permanecen en las trazas. Una pregunta sale como `answered`; una ambigüedad bloqueante se guarda como `needs_clarification`. No se solicita razonamiento privado.

Si `E2B_API_KEY` está configurada, las propuestas con Python se escriben en un sandbox desechable sin acceso a Internet ni variables de la aplicación. Se ejecuta `compileall` y, si hay archivos `test*.py`, `unittest discover`; stdout y stderr se truncan. El sandbox se cierra al terminar. Los proyectos sin Python quedan `not_executed` en esta primera versión; no se presenta una comprobación vacía como exitosa. `validation_status` (`passed`, `failed`, `error` o `not_executed`) es independiente de la decisión del Reviewer. Los archivos HTML aprobados pueden verse en un iframe sin `allow-same-origin`, con CSP que bloquea recursos externos y conexiones. El código generado nunca se sirve como página privilegiada del origen de la API.

## Pruebas

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Las pruebas usan un proveedor de modelo simulado y un sandbox E2B simulado; no crean recursos externos. Cubren aprobación, corrección, agotamiento, fallos, seguimiento, aclaraciones, autorización, límites y recuperación. **No validan calidad con un modelo real ni ejecución en E2B real.** Para una integración real, configure las claves en variables del servicio, arranque la API, envíe un ticket pequeño, consulte `/api/runs/{id}` hasta un estado terminal y revise `/traces` y `/files`. Revise manualmente la calidad del código y el estado de validación.

## Railway

1. En el proyecto Supabase `cartografunk` autorizado para compartir la instancia, aplique `migrations/001_initial.sql`, `migrations/002_sandbox_and_recovery.sql` y `migrations/20261007194652_batcomputer_app_role.sql`, en ese orden. Ya se aplicaron mediante el conector a ese proyecto. Las migraciones crean únicamente el esquema `batcomputer`, sus tablas y el rol `batcomputer_app`; no alteran tablas de la web principal. Son aditivas.
2. Antes de desplegar, genere una contraseña fuerte en un gestor de contraseñas y active el rol con `ALTER ROLE batcomputer_app WITH LOGIN PASSWORD '<contraseña>';` desde el editor SQL de Supabase. No incluya ese comando con la contraseña real en una migración ni en el repositorio. Cree `DATABASE_URL` en Railway con ese rol, no con el administrador `postgres`; para el pooler en modo Session, el usuario es `batcomputer_app.<project-ref>` y el host y puerto se copian del panel **Connect**. Mantenga `sslmode=require`.
3. Cree un servicio Railway Hobby desde `cartografunk/batcomputer`, rama `main`. Railway detecta el `Dockerfile` de la raíz. Configure una réplica y el healthcheck HTTP `/health` en Railway. El Dockerfile escucha en `0.0.0.0:$PORT`.
4. Configure en **Variables** del servicio: `DATABASE_URL`, `AUTH_TOKENS`, `MODEL_API_KEY`, `MODEL_NAME`, `MODEL_BASE_URL` si difiere del predeterminado, `GEMINI_API_KEY` y `GEMINI_ROLES` si se usará el modo mixto, `TAVILY_API_KEY` si se desea búsqueda y `E2B_API_KEY` si se desea ejecución aislada. Ajuste `CORS_ORIGINS` al origen público definitivo y los límites de `.env.example`. La imagen fija `APP_ENV=production` y exige PostgreSQL, autenticación y clave del modelo al arrancar. No copie secretos al repositorio ni al frontend.
5. Pruebe primero el dominio temporal de Railway: `/health`, creación de conversación, ticket real, trazas, validación y Flappy Bird. El mismo contenedor sirve FastAPI y `app/static`; para sustituir la interfaz, coloque el build del diseñador (`index.html` y `assets/`) en `app/static` antes de construir la imagen. Las rutas `/api`, `/health` y `/flappy` permanecen reservadas.

El worker está dentro del mismo servicio: no hace falta un proceso Railway adicional. Más réplicas aumentan consumo; el bloqueo de PostgreSQL y `MAX_CONCURRENT_RUNS` coordinan reclamos. El cierre espera hasta `SHUTDOWN_GRACE_SECONDS` al trabajo activo; si termina abruptamente, el lease y la recuperación acotada gestionan el trabajo interrumpido. El Dockerfile solo empaqueta la aplicación; el aislamiento de código lo aporta E2B. No se realizó ningún despliegue ni contratación.

## Cartografunk y dominio

| Repositorio | Función | Hosting previsto |
|---|---|---|
| `cartografunk/cartografunk_web` | Sitio existente en `www.cartografunk.com` | Hosting actual, sin cambios |
| `cartografunk/batcomputer` | Frontend, FastAPI, LangGraph, pruebas y documentación | Servicio independiente en Railway |

La dirección propuesta para la aplicación es `batcomputer.cartografunk.com`; **no está configurada**. El enlace llamado **Batcomputer** está preparado en una rama de trabajo independiente de `cartografunk/cartografunk_web` y no debe publicarse hasta que el subdominio funcione. La aplicación ya ofrece un enlace de regreso a `www.cartografunk.com`. No se modificaron DNS ni se publicó un despliegue. La identidad visual final seguirá el diseño que entregue el diseñador; la interfaz incluida es provisional y reemplazable.

Consulte [Dominio personalizado y DNS](docs/DOMAIN.md) para los pasos exactos de Railway, verificación y HTTPS. Los nombres y valores de los registros se copiarán de Railway cuando se configure el dominio; este repositorio no los inventa. La alternativa bajo una ruta de `www.cartografunk.com` queda fuera de esta arquitectura y necesitaría confirmar primero el proxy del hosting actual.

## Documentos

- [Contrato API](docs/API.md)
- [Decisiones y límites](docs/DECISIONS.md)
- [Dominio personalizado y DNS](docs/DOMAIN.md)
- [Criterios de aceptación de la prueba](docs/ACCEPTANCE.md)
