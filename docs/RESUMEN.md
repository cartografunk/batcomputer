# Resumen

## 1. Qué haría distinto

- **Verificaría contra infraestructura real antes de decir "listo".** La suite (115 pruebas) y la evaluación simulada usan un proveedor de modelo y un sandbox simulados, y SQLite. Las migraciones de PostgreSQL, el modelo real (Azure OpenAI), la ejecución en E2B y la interfaz en un navegador no se han verificado. Lo primero que haría es correr la evaluación con el modelo real y aplicar las migraciones en una copia de la base.
- **Adelgazaría las trazas.** En una medición con el proveedor simulado y entregas de unos 100 mil caracteres, las trazas ocuparon unas 3,6 veces más que los archivos útiles, porque guardan cada carga completa. Guardaría rutas y hashes en lugar del contenido y definiría una retención. No se hizo por tiempo; es la fase 6 del plan de seguimiento.
- **Ligaría cada mensaje del asistente a su ejecución.** Hoy un fallo temporal y el resultado posterior de un reintento quedan los dos en el historial.
- **No añadiría vectores ni un data lake.** Una entrega cabe entera en el contexto (máximo 30 archivos y 150 mil caracteres) y el seguimiento necesita los bytes exactos, no una búsqueda por similitud. Lo reconsideraría solo si se levanta ese límite o se pide búsqueda entre conversaciones.
- **Decisión de diseño, no hallazgo original:** el seguimiento usa snapshots inmutables de archivos con punteros a la última entrega aprobada y al último candidato rechazado, y un bucle revisor/generador con tope. Son patrones conocidos (snapshots al estilo de git); lo adoptamos como elección de diseño.

## 2. Qué pasa cuando Reviewer y Coder no coinciden, y por qué ese límite

El Reviewer actúa como project manager: compara la entrega con cada criterio de aceptación y con el resultado del Tester. Si la rechaza, devuelve su feedback al Coder, que entrega una versión completa nueva. Esto se repite hasta `MAX_CODER_ATTEMPTS` (3: el primer intento más dos correcciones).

Si el tercer intento también se rechaza, la ejecución termina en `exhausted`, **nunca como aprobada**. El mensaje empieza con la línea fija "Se alcanzó el límite de intentos de revisión. Error persistente detectado." y añade hasta cinco puntos del último feedback del Reviewer, de 300 caracteres como máximo cada uno. La entrega rechazada se conserva como candidato, pero no reemplaza a la última aprobada. Si el usuario pide corregirla, el Router puede elegir ese candidato como base.

El límite acota el gasto y evita ciclos improductivos: un Coder y un Reviewer que no convergen en tres vueltas es poco probable que lo hagan en una cuarta con el mismo feedback. Es un criterio de diseño que no medimos con un modelo real. El valor es configurable, pero el código lo restringe entre 1 y 3.
