# Resumen

## 1. Qué haría distinto con más tiempo

### Validar ejecutando el código, no solo leyéndolo

Hoy el Tester revisa sintaxis de Python y JSON y no ejecuta JavaScript. Con E2B configurado correría pruebas en un sandbox aislado a nivel de kernel utilizando microVMs (Particula Tech, 2026). Sin eso, un juego que carga pero no funciona/compila podría pasar la revisión, ya que la evaluación de un modelo no sustituye la validación estricta de un entorno de ejecución real (Penligent, 2026). Creo que es urgente a agregar en la lista de los requisitos del sistema, por lo que sería lo primero que cerraría antes de lanzar el aplicativo.

### Medir calidad con el modelo que mida calidad

La suite de pruebas usa un proveedor y un sandbox simulados. Faltaría correr la batería completa y publicar sus resultados. Lo que estas pruebas no miden es la calidad del código que escribe un modelo real. Para eso existe la batería python -m evals, que se corre contra el modelo real y que solo ejecuté de forma manual y parcial en producción. Correrla completa y publicar sus resultados es lo primero que haría con más tiempo.

### Adelgazar las trazas

Guardan cada carga completa para evitar esto y optimizar se deberían guardar rutas y hashes en lugar del contenido y definiría una retención. Esto evita el fenómeno de "podredumbre del contexto" o context rot (Simutecra, 2025) y aplica el patrón de reinicio de contexto (context reset) necesario en aplicaciones prolongadas (Zhou, 2026).

### Ligar cada mensaje del asistente a su ejecución

Hoy un fallo temporal y el resultado de un reintento quedan en el historial. Las convenciones modernas de observabilidad dictan que una transacción lógica debe englobar silenciosamente sus reintentos por fallos transitorios en el backend, en lugar de arrastrarlos como ruido al historial conversacional (IoT Digital Twin PLM, 2026). Esto sucede como una medida para evitar fallos silenciosos lo cual fue deseable para esta prueba.

### Un Reviewer de otra familia de modelos (por ejemplo Gemini)

Incluir un reviewer de otra familia reduce el riesgo de que el mismo modelo apruebe su propio estilo. La literatura advierte que los modelos sobrevaloran su propio trabajo, por lo que un evaluador externo e independiente es imperativo (Penligent, 2026; Zhou, 2026). Por practicidad se trabajo todo con el acceso otorgado de OpenAI, sin embargo la suite de Railway y Codex son muy amigables para incluir modelos de otros proveedores.

### Lo que no añadiría: vectores ni data lake

Una entrega cabe completa en el contexto y el seguimiento necesita los bytes exactos, no aproximaciones semánticas. Tratando el contexto como un presupuesto estricto de memoria (Machine Learning Mastery, 2025), el uso innecesario de sistemas RAG puede inyectar datos irrelevantes en la memoria de trabajo del agente. Lo reconsideraría solo si se pide búsqueda histórica.

### Diseño estricto y prompteo explícito

Hoy el Planner prefiere preguntar cuando falta una decisión que bloquea la implementación, en lugar de asumirla en silencio. Es una elección de diseño deliberada: reduce resultados no deseados y evita que el código derive hacia el scope creep, es decir, que crezca más allá de lo pedido (PMI, 2021). Para un usuario principiante esto es deseable, pero un usuario avanzado preferiría que el sistema continuara con supuestos razonables, documentándolos, sin interrumpirlo. Por eso haría este comportamiento configurable por usuario, por ejemplo con un modo “preguntar” y un modo “asumir y documentar”.

## 2. Qué pasa cuando Reviewer y Coder no coinciden, y por qué ese límite

### Comportamiento

El Reviewer actúa como project manager: compara la entrega con cada criterio de aceptación y con el resultado del Tester. Si la rechaza, devuelve su feedback al Coder, que entrega una versión completa nueva. Esto se repite hasta MAX_CODER_ATTEMPTS, que vale 3.

Si el tercer intento también se rechaza, la ejecución termina en exhausted, nunca como aprobada:

El mensaje empieza con la línea fija "Se alcanzó el límite de intentos de revisión. Error persistente detectado", implementando un patrón estructurado de límite de pérdidas o Stop-Loss (Zhou, 2026), y añade hasta cinco puntos del último feedback del Reviewer.

La entrega rechazada se conserva como candidato, pero no reemplaza a la última aprobada.

Los fallos técnicos del modelo (timeout, 429, respuesta malformada) se reintentan de forma aislada con espera exponencial y, si persisten, el sistema queda en estado paused con botón de reintento.

### Por qué 3

Con un solo reintento casi no hay oportunidad de corregir. Más de tres gasta tokens sin mejorar casi nada. La evidencia académica demuestra que añadir iteraciones multiagente en tareas de satisfacción de restricciones secuenciales genera rendimientos severamente decrecientes (OpenReview, 2025). Asimismo, los bucles prolongados sin convergencia provocan "deriva arquitectónica", donde el código muta erráticamente (arXiv, 2026; Yin, 2026). El límite acota costo y latencia, y el estado explícito evita que un fallo se presente como éxito.

### Qué no sé todavía

Elegí el 3 por razonamiento de diseño, no lo medí empíricamente con un modelo real. El valor es configurable, pero el código lo restringe entre 1 y 3.

## Referencias Bibliográficas

- CodeDex. (2026). Build a Flappy Bird Clone with Phaser (HTML5 Canvas).
- IoT Digital Twin PLM. (2026). OpenTelemetry GenAI Semantic Conventions and Logical Operations.
- Machine Learning Mastery. (2025). Effective Context Engineering for AI Agents: A Developer's Guide.
- OpenReview / arXiv. (2025). Multi-Agent Systems and Diminishing Returns in Sequential Constraint Satisfaction. (arXiv:2512.08296v3).
- Particula Tech. (2026). Modal vs E2B vs Daytona: Sandbox Execution for AI Agents.
- Penligent. (2026). Claude Code Harness for AI Pentesting: Verifier Separation.
- Project Management Institute. (2021). A guide to the project management body of knowledge (PMBOK guide) (7th ed.). Project Management Institute.
- Simutecra. (2025). Context Engineering for CAD and Prevention of Context Rot.
- Yin, M. (2026). Harness Engineering for AI Agents and Architectural Drift. Milvus Blog.
- Zhou, J. (2026). Harness Engineering: What OpenAI and Anthropic Changed. AI Builder Club.
