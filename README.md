# Nova Desktop

Nova es un asistente virtual local para Windows que combina un modelo local mediante Ollama con herramientas de escritorio, navegador, memoria, proyectos y automatización de tareas.

## Estado actual

**Nova v0.6.6 — Self Repair + Performance Profiler**

GitHub es la fuente oficial del proyecto:

- el código administrado por el updater vive en `nova/`;
- `main` contiene el desarrollo estable integrado;
- las versiones estables se publican como GitHub Releases (`v0.x.y`);
- el updater sincroniza directamente los archivos del tag publicado, sin ZIPs;
- cada archivo se verifica contra su Git blob SHA;
- se crea un backup antes de reemplazar archivos y existe rollback ante fallos de validación.

## v0.6.6 — Self Repair + Performance Profiler

Nova Doctor puede proponer reparaciones deterministas para fallos conocidos y ejecutarlas desde su ventana únicamente después de una confirmación explícita. Entre otras cosas puede reparar dependencias, re-sincronizar archivos gestionados, iniciar Ollama, descargar modelos y ayudar con GitHub/Playwright.

El Performance Profiler guarda métricas exclusivamente locales en `data/performance.db`: nombre de operación, duración, éxito y metadatos técnicos pequeños. No almacena prompts, mensajes, secretos ni contenido de archivos.

Puedes preguntar `Nova, ¿cómo va tu rendimiento?` para ver promedios y cuellos de botella recientes.

El atajo global por defecto es ahora **Ctrl + Alt + Espacio**. Las instalaciones que todavía tengan exactamente el antiguo `Ctrl + Espacio` se migran automáticamente; otros atajos personalizados se respetan.

## v0.6.5 — Continuity Engine

Nova mantiene sesiones de trabajo persistentes por workspace y checkpoints estructurados con objetivo, pasos completados, pendientes, archivos, decisiones y errores. Esto permite órdenes como `Nova, continúa`, `¿dónde nos quedamos?`, `¿qué quedó pendiente?` y `¿qué hicimos ayer?`.

Continuity guarda estado temporal/accionable; Semantic Memory sigue reservada para hechos y decisiones duraderas.

## v0.6.3 — Semantic Memory

La recuperación de recuerdos deja de depender solamente de palabras exactas. `memory_search` combina ranking léxico, similitud semántica, importancia, recencia y prioridad del workspace activo.

Los embeddings se generan localmente mediante Ollama y se guardan en la misma base SQLite de Nova. Por defecto se usa `qwen3-embedding:0.6b`, pero el modelo es configurable.

Nova no descarga el modelo automáticamente. Si el modelo no está instalado o Ollama no responde, la búsqueda vuelve inmediatamente al sistema léxico de v0.6 sin impedir que el asistente funcione.

Herramientas nuevas:

- `memory_semantic_status`: muestra modelo, disponibilidad, recuerdos indexados y pendientes;
- `memory_semantic_reindex`: regenera embeddings locales cuando se solicita;
- `memory_search`: usa automáticamente búsqueda híbrida cuando Semantic Memory está disponible.

Nova Doctor también informa el estado de Semantic Memory.

## v0.6.2 — Update Reliability

Las actualizaciones iniciadas desde `⬆ Actualizar` pasan por un supervisor independiente (`updater/update_runner.py`). El supervisor espera el cierre de la instancia actual, ejecuta el updater, guarda un log local y vuelve a iniciar Nova tanto si la actualización termina correctamente como si falla.

El resultado se guarda en `data/update_last.json` y Nova lo muestra al siguiente arranque. Los logs quedan en `data/updater_logs/` para que un fallo ya no deje la aplicación cerrada sin explicación.

`ACTUALIZAR_NOVA.cmd` usa el mismo supervisor, de modo que el flujo desde la interfaz y el flujo manual comparten la misma lógica de actualización y reinicio.

## v0.6.1 — Workspace Intelligence

Cada workspace puede mantener un índice local incremental en SQLite. Nova puede detectar archivos añadidos, modificados y eliminados y buscar rutas por nombre sin recorrer de nuevo todo el proyecto.

Herramientas nuevas:

- `workspace_index`: crea/actualiza el índice del proyecto;
- `workspace_changes`: resume los cambios detectados en el último análisis;
- `workspace_search`: busca archivos dentro del índice local;
- `workspace_index_status`: muestra el estado y tamaño del índice.

El indexador está limitado por profundidad y cantidad de archivos, ignora carpetas pesadas como `.git`, `.venv`, `node_modules`, caches y builds, y solo calcula SHA-256 para archivos pequeños/importantes cuando aporta valor para distinguir cambios reales.

## v0.6.0 — Memory & Workspace

Nova mantiene un **workspace activo** que representa el proyecto con el que estás trabajando. Guarda ruta, tipo de proyecto, metadatos básicos, memorias asociadas y continuidad de tareas.

Ejemplos:

- `mi servidor` puede referirse al workspace activo del servidor Minecraft;
- una tarea creada mientras un workspace está activo queda asociada a ese proyecto;
- Nova recupera solo las memorias relevantes para la petición actual;
- las rutas de workspaces registrados pasan a ser raíces de trabajo reconocidas por Nova.

Tipos detectados inicialmente: Nova, servidor Minecraft, Python, Node, Arduino, Godot, Unity, Visual Studio, Git y genérico.

La interfaz incorpora `📁 Proyectos`, `🩺 Doctor` y `⬆ Actualizar`.

## Migración del núcleo

La rama 0.6 continúa usando una capa de compatibilidad administrada desde GitHub sobre algunos módulos históricos v0.5 ya instalados localmente. La migración completa del núcleo continuará en versiones posteriores para evitar reemplazos masivos y riesgos innecesarios.

## Estructura

```text
nova-desktop/
├─ nova/
│  ├─ assistant/
│  │  ├─ memory.py
│  │  ├─ semantic_memory.py
│  │  ├─ continuity.py
│  │  ├─ profiler.py
│  │  ├─ self_repair.py
│  │  ├─ workspace.py
│  │  ├─ workspace_index.py
│  │  ├─ doctor.py
│  │  ├─ v060_*.py
│  │  ├─ v061_*.py
│  │  ├─ v063_*.py
│  │  ├─ v065_*.py
│  │  └─ v066_*.py
│  ├─ updater/
│  │  ├─ nova_updater.py
│  │  └─ update_runner.py
│  ├─ app.py
│  └─ ...
├─ tests/
├─ .github/workflows/
├─ VERSION
├─ CHANGELOG.md
└─ README.md
```

## Privacidad

Nunca deben subirse al repositorio `config.json` real, `assistant.db`, `performance.db`, `data/`, perfiles del navegador, capturas, logs personales, `.venv/`, claves API, tokens o credenciales.

`nova/config.example.json` contiene únicamente valores de ejemplo/por defecto. Los embeddings y las métricas de rendimiento se guardan exclusivamente de forma local.

## Publicación

Los pull requests ejecutan compilación y pruebas. Al fusionar una versión con un nuevo `VERSION`, GitHub Actions vuelve a validar el código y crea la Release correspondiente si todavía no existe.
