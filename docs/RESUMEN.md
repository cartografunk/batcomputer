# Resumen

## 1. Qué haría distinto

- **Completaría la validación de ejecución antes de decir "listo".** La suite (115 pruebas) usa un proveedor de modelo y un sandbox simulados; el arnés de PostgreSQL local pasó seis pruebas. En producción, Azure OpenAI aprobó tickets reales de cupones y calculadora, una modificación conservó los archivos anteriores y una pregunta terminó sin generar código. La interfaz y Flappy Bird se abrieron en navegador. La validación del código generado quedó en `not_executed` porque E2B no está configurado; faltan esa ejecución y una evaluación completa de calidad con el modelo real.
- **Adelgazaría las trazas.** En una medición con el proveedor simulado y entregas de unos 100 mil caracteres, las trazas ocuparon unas 3,6 veces más que los archivos útiles, porque guardan cada carga completa. Guardaría rutas y hashes en lugar del contenido y definiría una retención. No se hizo por tiempo; es la fase 6 del plan de seguimiento.
- **Ligaría cada mensaje del asistente a su ejecución.** Hoy un fallo temporal y el resultado posterior de un reintento quedan los dos en el historial.
- **No añadiría vectores ni un data lake.** Una entrega cabe entera en el contexto (máximo 30 archivos y 150 mil caracteres) y el seguimiento necesita los bytes exactos, no una búsqueda por similitud. Lo reconsideraría solo si se levanta ese límite o se pide búsqueda entre conversaciones.
- **Decisión de diseño, no hallazgo original:** el seguimiento usa snapshots inmutables de archivos con punteros a la última entrega aprobada y al último candidato rechazado, y un bucle revisor/generador con tope. Son patrones conocidos (snapshots al estilo de git); lo adoptamos como elección de diseño.

## 2. Qué pasa cuando Reviewer y Coder no coinciden, y por qué ese límite

El Reviewer actúa como project manager: compara la entrega con cada criterio de aceptación y con el resultado del Tester. Si la rechaza, devuelve su feedback al Coder, que entrega una versión completa nueva. Esto se repite hasta `MAX_CODER_ATTEMPTS` (3: el primer intento más dos correcciones).

Si el tercer intento también se rechaza, la ejecución termina en `exhausted`, **nunca como aprobada**. El mensaje empieza con la línea fija "Se alcanzó el límite de intentos de revisión. Error persistente detectado." y añade hasta cinco puntos del último feedback del Reviewer, de 300 caracteres como máximo cada uno. La entrega rechazada se conserva como candidato, pero no reemplaza a la última aprobada. Si el usuario pide corregirla, el Router puede elegir ese candidato como base.

El límite acota el gasto y evita ciclos improductivos: un Coder y un Reviewer que no convergen en tres vueltas es poco probable que lo hagan en una cuarta con el mismo feedback. Es un criterio de diseño que no medimos con un modelo real. El valor es configurable, pero el código lo restringe entre 1 y 3.
