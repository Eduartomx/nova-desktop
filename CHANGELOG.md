# Changelog

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
- Detección rápida de proyectos: Nova, Minecraft server, Python, Node, Arduino, Godot, Unity, Visual Studio y Git.
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
