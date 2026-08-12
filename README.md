# Nova Desktop

Nova es un asistente virtual local para Windows que combina un modelo local mediante Ollama con herramientas de escritorio, navegador, memoria, proyectos, continuidad, percepción contextual y habilidades reutilizables.

## Estado actual

**Nova v0.8.0 — Skills Engine**

GitHub es la fuente oficial del proyecto:

- el código administrado por el updater vive en `nova/`;
- `main` contiene el desarrollo estable integrado;
- las versiones estables se publican como GitHub Releases (`v0.x.y`);
- el updater sincroniza directamente los archivos del tag publicado, sin ZIPs;
- cada archivo se verifica contra su Git blob SHA;
- se crea un backup antes de reemplazar archivos y existe rollback ante fallos de validación.

## v0.8.0 — Skills Engine

Skills Engine permite guardar procedimientos repetibles como **playbooks declarativos**.

Una Skill puede contener:

- nombre y descripción;
- frases trigger;
- parámetros tipados;
- pasos ordenados;
- herramienta sugerida por paso;
- verificación por paso y final;
- alcance global o por workspace;
- capacidades requeridas de forma declarativa;
- versión, origen e historial de ejecuciones.

Una Skill **no es un script**. No ejecuta Python/PowerShell por sí sola y el campo `permissions` nunca concede permisos: toda acción vuelve a pasar por Agent/LocalTools y por la política normal de seguridad de Nova.

Ejemplos:

```text
Nova, ¿qué habilidades tienes?
Nova, estado de habilidades.
Nova, usa la habilidad Reiniciar servidor server=Alpha.
Nova, guarda este procedimiento como una habilidad llamada Reparar servidor.
```

Herramientas:

- `skill_status`;
- `skill_list`;
- `skill_search`;
- `skill_info`;
- `skill_save`;
- `skill_run`;
- `skill_finish`;
- `skill_set_enabled`;
- `skill_runs`.

La interfaz añade **🧩 Skills** para revisar habilidades, versiones, alcance, confianza, pasos y habilitarlas/deshabilitarlas.

### Confianza de Skills

- `draft`: generada por Nova o todavía sin historial suficiente;
- `user`: guardada a petición explícita del usuario;
- `verified`: una Skill `draft` con al menos dos ejecuciones distintas verificadas correctamente.

La confianza nunca modifica permisos.

### Privacidad y seguridad de Skills

Las definiciones que parecen contener claves privadas, API keys, tokens u otros secretos conocidos se rechazan. Parámetros sensibles (`password`, `token`, `secret`, `cookie`, `api_key`, etc.) se redactan en SQLite y no se interpolan dentro del playbook.

`data/skills.db` guarda definiciones, revisiones y metadatos técnicos de ejecución. Los pasos registrados durante una ejecución son los templates originales y, por defecto, no se persiste el resumen completo de lo que respondió el Agent.

`auto_execute_matches=false` por defecto: una coincidencia léxica puede sugerir una Skill, pero no dispara una rutina automáticamente.

Más detalles: `docs/v0.8.0-skills-engine.md`.

## Percepción 0.7

La rama 0.7 añadió percepción incremental sin convertir Nova en un sistema que observa la pantalla continuamente.

### v0.7.0 — Perception Engine

Mantiene un contexto barato del escritorio observando metadatos de ventana/proceso activo y carga básica del sistema. Conserva la última aplicación externa cuando Nova pasa al frente y puede relacionarla con un workspace probable.

### v0.7.1 — Context Intelligence

Reduce rebotes entre aplicaciones, infiere actividad probable (`programming`, `gaming`, `browsing`, etc.) y puntúa relevancia antes de añadir cambios al contexto del agente.

### v0.7.2 — Workspace Auto-Detection

Aprende asociaciones locales aplicación ↔ workspace mediante evidencia acumulada. Un título de ventana aislado no entrena la asociación y la activación automática del workspace permanece deshabilitada por defecto.

### v0.7.3 — Anomaly Detection

Aprende líneas base de CPU/RAM por actividad y proceso. Busca desviaciones sostenidas, procesos inesperadamente pesados y señales de Windows Error Reporting sin matar, bloquear o modificar procesos automáticamente.

### v0.7.4 — Event-driven Vision

Añade visión local como **fallback por evento**, no como observación continua.

Nova puede usar una captura cuando el usuario lo pide explícitamente o aparece un evento visual permitido; por defecto solo una `crash_signal` de Anomaly Detection. No existe un hilo de screenshots periódicos.

El análisis se hace mediante Ollama y solo si el modelo configurado informa capability `vision`. Nova no descarga un modelo automáticamente y no usa OpenAI como fallback automático.

Por defecto las imágenes se procesan en memoria y no se conservan (`retain_images=false`), el texto completo del análisis no se persiste (`persist_analysis=false`) y el contenido visual se trata como dato externo/no confiable.

## Memory, Workspace y Continuity

Nova mantiene un **workspace activo** que representa el proyecto con el que estás trabajando. Las tareas, recuerdos, Skills y checkpoints pueden asociarse al proyecto.

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

## Núcleo consolidado

Desde v0.6.7 Nova usa un único bootstrap `assistant.core_runtime.install_core_runtime()`.

Los módulos históricos locales `agent.py`, `tools.py`, `ui.py` y `task_engine.py` todavía se mantienen como contrato legacy y reciben adaptadores estables por dominio. Los nuevos motores persistentes (`memory`, `continuity`, `perception`, `skills`, etc.) viven como módulos administrados por GitHub.

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
│  │  ├─ anomaly.py
│  │  ├─ event_vision.py
│  │  ├─ skills.py
│  │  ├─ profiler.py
│  │  ├─ self_repair.py
│  │  ├─ workspace.py
│  │  ├─ workspace_index.py
│  │  ├─ doctor.py
│  │  ├─ agent_*.py
│  │  ├─ tools_*.py
│  │  └─ ui_*.py
│  ├─ updater/
│  ├─ app.py
│  └─ ...
├─ tests/
├─ docs/
├─ .github/workflows/
├─ VERSION
├─ CHANGELOG.md
└─ README.md
```

## Privacidad general

Nunca deben subirse al repositorio `config.json` real, `assistant.db`, `skills.db`, `performance.db`, `perception.db`, `vision_events.db`, `data/`, perfiles del navegador, capturas, logs personales, `.venv/`, claves API, tokens o credenciales.

`nova/config.example.json` contiene únicamente valores de ejemplo/por defecto. Los datos personales y operativos permanecen locales salvo que una herramienta externa sea invocada explícitamente para una tarea.

## Publicación

Los pull requests ejecutan compilación y pruebas. Al fusionar una versión con un nuevo `VERSION`, GitHub Actions vuelve a validar el código y crea la Release correspondiente si todavía no existe.
