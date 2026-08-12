# Nova Desktop

Nova es un asistente virtual local para Windows que combina un modelo local mediante Ollama con herramientas de escritorio, navegador, memoria, proyectos, continuidad de tareas y percepción contextual del PC.

## Estado actual

**Nova v0.7.0 — Perception Engine**

GitHub es la fuente oficial del proyecto:

- el código administrado por el updater vive en `nova/`;
- `main` contiene el desarrollo estable integrado;
- las versiones estables se publican como GitHub Releases (`v0.x.y`);
- el updater sincroniza directamente los archivos del tag publicado, sin ZIPs;
- cada archivo se verifica contra su Git blob SHA;
- se crea un backup antes de reemplazar archivos y existe rollback ante fallos de validación.

## v0.7.0 — Perception Engine

Perception Engine mantiene un contexto barato del escritorio sin mantener visión encendida. Por defecto observa cada ~1,1 s únicamente metadatos de la ventana/proceso activo y carga básica del sistema.

Puede conservar la última aplicación externa cuando Nova pasa al frente, clasificar el tipo de aplicación y relacionar el contexto con un workspace registrado. El workspace probable es solo una inferencia y **no se activa automáticamente** por defecto.

Ejemplos:

- `Nova, ¿qué aplicación tengo abierta?`
- `¿Qué estaba usando antes de abrir Nova?`
- `¿Está funcionando tu percepción?`
- `¿Qué cambios de contexto viste recientemente?`

Herramientas nuevas:

- `perception_context`: contexto estructurado actual;
- `perception_status`: estado y garantías de privacidad;
- `perception_recent`: cambios recientes de aplicación/workspace/carga del sistema.

### Privacidad de percepción

Perception Engine v0.7.0:

- **no hace screenshots periódicos**;
- **no captura teclado**;
- **no lee portapapeles**;
- no necesita LLM para observar el contexto;
- guarda un historial local limitado en `data/perception.db`;
- no persiste títulos de ventana por defecto (`persist_window_titles=false`).

Los títulos visibles en el contexto del agente se tratan explícitamente como **datos externos/no confiables**, nunca como instrucciones.

## Núcleo 0.6 consolidado

Desde v0.6.7 Nova usa un único bootstrap `assistant.core_runtime.install_core_runtime()`. MemoryStore integra de forma nativa Workspaces, Semantic Memory y Continuity; Nova Doctor integra Self Repair y Performance Profiler.

Los módulos históricos locales `agent.py`, `tools.py`, `ui.py` y `task_engine.py` todavía se mantienen como contrato legacy y reciben adaptadores estables por dominio. Esto permite continuar la migración sin reemplazar de golpe módulos que existían antes de que GitHub se convirtiera en la fuente oficial.

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

Nunca deben subirse al repositorio `config.json` real, `assistant.db`, `performance.db`, `perception.db`, `data/`, perfiles del navegador, capturas, logs personales, `.venv/`, claves API, tokens o credenciales.

`nova/config.example.json` contiene únicamente valores de ejemplo/por defecto. Memoria, embeddings, métricas y percepción permanecen locales salvo que una herramienta externa sea invocada explícitamente para una tarea.

## Publicación

Los pull requests ejecutan compilación y pruebas. Al fusionar una versión con un nuevo `VERSION`, GitHub Actions vuelve a validar el código y crea la Release correspondiente si todavía no existe.
