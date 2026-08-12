# Changelog

## v0.6.5 — Continuity Engine

- Nuevas sesiones persistentes de trabajo por workspace mediante `continuity_sessions`.
- Checkpoints estructurados en `continuity_checkpoints`: resumen, completado, pendientes, archivos, decisiones, errores y metadatos.
- Las tareas del Task Engine abren una sesión automáticamente y generan checkpoints al completar/fallar/pausar/bloquear pasos o tareas.
- Routing determinista para `Nova, continúa`, `¿dónde nos quedamos?`, `¿qué quedó pendiente?` y `¿qué hicimos ayer?`.
- `Nova, continúa` reconstruye primero el estado local y solo después entra al agente normal, evitando repetir trabajo ya completado.
- Nuevas herramientas: `continuity_resume`, `continuity_pending`, `continuity_history`, `continuity_checkpoint` y `continuity_close`.
- El system prompt recibe un contexto compacto de continuidad del workspace activo, separado de Semantic Memory.
- La interfaz avisa al iniciar si el workspace activo tiene trabajo pendiente y permite retomarlo con lenguaje natural.
- Continuity es tolerante a fallos: un problema al guardar un checkpoint nunca debe romper el Task Engine.
- Nuevas pruebas de persistencia tras reabrir SQLite, checkpoints automáticos, cierre de sesiones y routing.

## v0.6.4 — Semantic Routing Hotfix

- Corrige el comando explícito `reindexa la memoria semántica`, que podía caer en el Task Engine y confundirse con Windows Search/SearchIndexer.
- Añade routing determinista previo al LLM para `memory_semantic_reindex` y `memory_semantic_status`.
- Las órdenes de Semantic Memory ya no usan PowerShell ni servicios de indexación de Windows.
- Si falta el modelo de embeddings, Nova devuelve directamente el comando `ollama pull` correcto en vez de intentar una vía alternativa del sistema operativo.
- Soporta variantes con/sin acentos y comandos limitados al workspace/proyecto actual.
- Añade una regla explícita al system prompt como segunda barrera contra confusiones futuras.
- Nuevas pruebas de regresión para routing semántico y para evitar interceptar órdenes reales de Windows Search.

## v0.6.3 — Semantic Memory

- Búsqueda híbrida de recuerdos: coincidencia léxica + similitud semántica + importancia + recencia + prioridad del workspace activo.
- Nuevo índice `memory_embeddings` dentro de la misma base SQLite local de Nova.
- Embeddings generados localmente mediante Ollama `/api/embed`; no se envían recuerdos a servicios externos.
- Modelo por defecto: `qwen3-embedding:0.6b`, configurable desde `semantic_memory`.
- Nova **no descarga automáticamente** el modelo de embeddings; si falta, mantiene el buscador léxico de v0.6 como fallback inmediato.
- Indexado por lotes, invalidación automática cuando cambia un recuerdo y lazy indexing limitado para evitar reindexar toda la memoria en cada consulta.
- Nuevas herramientas: `memory_semantic_status` y `memory_semantic_reindex`; `memory_search` usa automáticamente el ranking híbrido cuando está disponible.
- Nova Doctor informa modelo, disponibilidad e índice semántico.
- La UI usa la versión real desde `NOVA_VERSION.txt` y muestra si Semantic Memory está activa o en fallback.
- Pruebas automáticas para fallback léxico, recuperación semántica sin palabras compartidas e invalidación de embeddings obsoletos.

## v0.6.2 — Update Reliability

- Nuevo `update_runner.py` que supervisa las actualizaciones lanzadas desde la interfaz.
- El botón `⬆ Actualizar` deja de depender de una cadena `cmd.exe && start` frágil.
- Nova espera a que la instancia actual termine antes de reemplazar archivos administrados.
- Cada actualización guarda un log local en `data/updater_logs/`.
- El resultado se registra en `data/update_last.json` y se muestra al volver a abrir Nova.
- Nova se vuelve a abrir incluso si el updater falla, evitando quedar cerrada sin explicación.
- El relanzado usa `INICIAR.bat` cuando existe y tiene fallback directo a `pythonw.exe app.py`.
- `ACTUALIZAR_NOVA.cmd` usa el mismo supervisor para que el flujo manual y el botón tengan el mismo comportamiento.
- Pruebas automáticas para estado, versión y selección del intérprete del supervisor.

## v0.6.1 — Workspace Intelligence

- Índice incremental local por workspace guardado en SQLite.
- Detección de archivos añadidos, modificados y eliminados entre análisis.
- Búsqueda rápida por nombre/ruta sin volver a recorrer todo el proyecto.
- Hash SHA-256 selectivo para archivos de configuración y código pequeños/importantes.
- Límites de profundidad y número de archivos para evitar escaneos costosos.
- Exclusión por defecto de `.git`, `.venv`, `node_modules`, caches y carpetas de build.
- Nuevas herramientas: `workspace_index`, `workspace_changes`, `workspace_search`, `workspace_index_status`.
- El selector de herramientas prioriza Workspace Intelligence ante preguntas sobre archivos y cambios.
- Nuevas pruebas automáticas de indexado incremental, búsqueda y liberación de SQLite.

## v0.6.0 — Memory & Workspace

- Nuevo sistema de **Workspaces** persistentes con proyecto activo.
- Detección rápida de proyectos: Nova, Minecraft server, Python, Node, Arduino, Godot, Unity, Visual Studio, Git y genérico.
- Metadatos específicos para servidores Minecraft: mods y propiedades básicas.
- Memoria enriquecida con categoría, alcance global/workspace, importancia y fuente.
- Búsqueda local de memoria relevante sin invocar al LLM.
- El prompt usa solo recuerdos relevantes para reducir contexto y latencia.
- Las tareas del Task Engine se asocian automáticamente al workspace activo.
- Nuevas herramientas: `memory_search`, `workspace_list`, `workspace_create`, `workspace_set_active`, `workspace_info`, `workspace_open`.
- Gestor visual de proyectos desde la interfaz.
- Nova Doctor rápido y determinista, sin usar el modelo local.
- Botón `Actualizar` integrado en Nova para instalar desde GitHub y reiniciar tras una actualización correcta.
- Capa de compatibilidad v0.6 administrada desde GitHub para extender de forma segura instalaciones v0.5 mientras continúa la migración completa del núcleo.
- Pruebas automáticas de Memory/Workspace y liberación del archivo SQLite.

## v0.5.8 — GitHub Native Updates

- GitHub pasa a ser la fuente de verdad del código de Nova.
- El código instalable se publica bajo `nova/`.
- El updater deja de depender de ZIPs y sincroniza archivos directamente desde el tag estable.
- Las consultas de Releases usan GitHub CLI autenticado para evitar el rate limit anónimo.
- Cada archivo descargado se valida contra su Git blob SHA.
- Backup y rollback automático antes de reemplazar archivos.
- Actualización de dependencias solo cuando cambia `requirements.txt`.
- Validación de sintaxis Python después de actualizar.

## v0.5.7 — GitHub Update Infrastructure

- Primer repositorio y Release oficial de Nova.
- Integración inicial con GitHub Releases.
