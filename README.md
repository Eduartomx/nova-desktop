# Nova Desktop

Nova es un asistente virtual local para Windows que combina un modelo local mediante Ollama con herramientas de escritorio, navegador, memoria, proyectos, continuidad de tareas y percepción contextual del PC.

## Estado actual

**Nova v0.7.4 — Event-driven Vision**

GitHub es la fuente oficial del proyecto:

- el código administrado por el updater vive en `nova/`;
- `main` contiene el desarrollo estable integrado;
- las versiones estables se publican como GitHub Releases (`v0.x.y`);
- el updater sincroniza directamente los archivos del tag publicado, sin ZIPs;
- cada archivo se verifica contra su Git blob SHA;
- se crea un backup antes de reemplazar archivos y existe rollback ante fallos de validación.

## Percepción 0.7

La rama 0.7 añade percepción incremental sin convertir Nova en un sistema que observa la pantalla continuamente.

### v0.7.0 — Perception Engine

Mantiene un contexto barato del escritorio observando cada ~1,1 s únicamente metadatos de ventana/proceso activo y carga básica del sistema. Puede conservar la última aplicación externa cuando Nova pasa al frente y relacionarla con un workspace probable.

### v0.7.1 — Context Intelligence

Reduce rebotes entre aplicaciones, infiere actividad probable (`programming`, `gaming`, `browsing`, etc.) y puntúa la relevancia de los cambios antes de añadirlos al contexto del agente.

### v0.7.2 — Workspace Auto-Detection

Aprende asociaciones locales aplicación ↔ workspace mediante evidencia acumulada. Un título de ventana aislado nunca entrena la asociación y la activación automática del workspace permanece deshabilitada por defecto.

### v0.7.3 — Anomaly Detection

Aprende líneas base de CPU/RAM por actividad y proceso. Busca desviaciones sostenidas, procesos inesperadamente pesados y señales de Windows Error Reporting sin matar, bloquear o modificar procesos automáticamente.

### v0.7.4 — Event-driven Vision

Añade visión local como **fallback por evento**, no como observación continua.

Nova puede usar una captura cuando:

- el usuario lo pide explícitamente (`Nova, ¿qué ves en mi pantalla?`);
- aparece un evento visual permitido por la política local; por defecto solo una `crash_signal` de Anomaly Detection.

No existe un hilo de screenshots periódicos. Las anomalías normales de CPU/RAM no disparan visión por defecto porque se explican mejor con métricas.

La captura intenta usar la última aplicación externa observada por Perception Engine para evitar analizar la propia ventana de Nova. El análisis se hace mediante Ollama y solo si el modelo configurado informa capability `vision`. Nova **no descarga un modelo automáticamente** y **no usa OpenAI como fallback automático**.

Herramientas:

- `vision_status`;
- `vision_describe_screen`;
- `vision_last`;
- `vision_recent_events`.

Consultas directas:

- `Nova, ¿qué ves en mi pantalla?`;
- `Nova, mira mi pantalla`;
- `¿qué error aparece en pantalla?`;
- `¿estado de tu visión por eventos?`;
- `¿cuál fue tu último análisis visual?`.

### Privacidad de Event-driven Vision

Por defecto:

- **no hay screenshots periódicos**;
- las imágenes se procesan en memoria y no se conservan (`retain_images=false`);
- el texto completo del análisis visual no se persiste (`persist_analysis=false`);
- `data/vision_events.db` guarda solo metadatos de ejecución, categoría y confianza;
- no captura teclado ni lee portapapeles;
- el contenido de la imagen se trata como dato externo/no confiable, nunca como instrucciones;
- el prompt visual prohíbe repetir contraseñas, tokens, cookies, claves API y otros secretos visibles.

## Memory, Workspace y Continuity

Nova mantiene un **workspace activo** que representa el proyecto con el que estás trabajando. Las tareas, recuerdos y checkpoints pueden asociarse al proyecto.

Workspace Intelligence mantiene un índice incremental de archivos para detectar cambios y localizar rutas sin recorrer el proyecto completo cada vez.

Semantic Memory combina recuperación léxica y embeddings locales de Ollama. El modelo por defecto es `qwen3-embedding:0.6b`; si falta, Nova vuelve automáticamente al buscador léxico.

Continuity Engine mantiene sesiones y checkpoints estructurados para órdenes como:

- `Nova, continúa`;
- `¿dónde nos quedamos?`;
- `¿qué quedó pendiente?`;
- `¿qué hicimos ayer?`.

## Doctor, Self Repair y rendimiento

Nova Doctor es determinista y no necesita LLM para diagnosticar. Puede proponer reparaciones conocidas con confirmación explícita antes de instalaciones, descargas o cambios relevantes.

Performance Profiler almacena métricas exclusivamente locales en `data/performance.db`: operación, duración, éxito y metadatos técnicos pequeños. No guarda prompts, mensajes, contraseñas, tokens ni contenido de archivos.

Puedes preguntar `Nova, ¿cómo va tu rendimiento?` para ver promedios y cuellos de botella recientes.

## Núcleo consolidado

Desde v0.6.7 Nova usa un único bootstrap `assistant.core_runtime.install_core_runtime()`. MemoryStore integra de forma nativa Workspaces, Semantic Memory y Continuity; Nova Doctor integra Self Repair y Performance Profiler.

Los módulos históricos locales `agent.py`, `tools.py`, `ui.py` y `task_engine.py` todavía se mantienen como contrato legacy y reciben adaptadores estables por dominio.

v0.7.4 también añade `assistant/anomaly_detection.py` como alias de compatibilidad del núcleo `assistant/anomaly.py`, corrigiendo la diferencia de nombre que podía afectar instalaciones 0.7.3.

## Atajos

El atajo global por defecto es **Ctrl + Alt + Espacio**. El push-to-talk predeterminado sigue siendo **F9**.

## Estructura

```text
nova-desktop/
├─ nova/
│  ├─ assistant/
│  │  ├─ core_runtime.py
│  │  ├─ memory.py
│  │  ├─ semantic_memory.py
│  │  ├─ continuity.py
│  │  ├─ perception.py
│  │  ├─ context_intelligence.py
│  │  ├─ workspace_autodetect.py
│  │  ├─ anomaly.py
│  │  ├─ event_vision.py
│  │  ├─ profiler.py
│  │  ├─ self_repair.py
│  │  ├─ workspace.py
│  │  ├─ workspace_index.py
│  │  ├─ doctor.py
│  │  ├─ agent_*.py
│  │  ├─ tools_*.py
│  │  └─ ui_*.py
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

## Privacidad general

Nunca deben subirse al repositorio `config.json` real, `assistant.db`, `performance.db`, `perception.db`, `anomaly_detection.db`, `vision_events.db`, `data/`, perfiles del navegador, capturas, logs personales, `.venv/`, claves API, tokens o credenciales.

`nova/config.example.json` contiene únicamente valores de ejemplo/por defecto. Memoria, embeddings, métricas, percepción y visión permanecen locales salvo que una herramienta externa sea invocada explícitamente para una tarea.

## Publicación

Los pull requests ejecutan compilación y pruebas. Al fusionar una versión con un nuevo `VERSION`, GitHub Actions vuelve a validar el código y crea la Release correspondiente si todavía no existe.
