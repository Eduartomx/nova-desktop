# Changelog

## v0.9.9 — Resident Mode & Runtime Lifecycle

- Añade `RuntimeLifecycleManager` nativo con estados `starting`, `running`, `hidden`, `shutting_down` y `stopped`, separando ocultar la ventana de terminar el proceso.
- El botón X usa `withdraw()` únicamente cuando Resident Mode está habilitado y la bandeja confirmó que está operativa; ocultar no detiene hotkeys, wake word, F9, Perception, Gaming Awareness ni descarga Qwen.
- `TrayController` respeta el contrato real de pystray: el callback personalizado de `run_detached(setup=...)` establece explícitamente `icon.visible = True` y solo confirma readiness después de verificar esa visibilidad.
- Cada intento de inicialización de bandeja usa su propio evento y generación; callbacks tardíos de intentos expirados son inertes y un timeout detiene únicamente el icono de ese intento.
- Si la bandeja falla, no confirma visibilidad, expira o se degrada después de ocultar Nova, Resident Mode restaura la ventana mediante el scheduler del lifecycle. `--background` nunca deja un proceso invisible sin icono operativo.
- El cierre real es idempotente y ejecuta una secuencia controlada. Un error de `root.after()` no impide el fallback síncrono `perform_shutdown_now()` y el lock físico sigue liberándose solo en el `finally` de `app.py`.
- La instancia única queda limitada a usuario + sesión de Windows. El scope se deriva del SID del usuario, persistiendo solo su hash, y Windows Session ID.
- `owner.json` se escribe atómicamente y registra PID, tiempo real de creación del proceso, `owner_id`/generación aleatoria, rol (`runtime`/`updater`), scope, hash de usuario, Session ID y timestamp. El lock del kernel sigue siendo la fuente de exclusión.
- `ProcessCapture` obtiene el tiempo de creación del proceso capturado; un PID solo se considera el propietario registrado si PID y tiempo de creación coinciden, evitando confundir PID reutilizados.
- Metadata obsoleta de runtime/updater se recupera de forma segura cuando el lock está libre; metadata antigua sin identidad fuerte falla cerrada cuando el lock está ocupado y nunca provoca comandos a un PID no verificable.
- El updater intenta adquirir directamente el guard exclusivo en vez de sondear/liberar/recompetir. Si una metadata anterior todavía identifica un proceso vivo, espera su terminación real mientras ya conserva el guard.
- El IPC residente usa archivos independientes por `command_id`, con `target_owner_id`, `command` y `created_at`. Solo acepta `show`, `shutdown_for_update` y `status`; no abre puertos ni servidores de red.
- Una generación nueva descarta comandos malformados, vencidos o dirigidos a otro `owner_id`; un `shutdown_for_update` abandonado por un crash nunca se ejecuta contra el runtime siguiente.
- Dos emisores concurrentes no se sobrescriben porque cada orden usa un archivo atómico independiente.
- Una segunda ejecución no construye Agent/Tk/servicios y solo devuelve éxito si pudo entregar `show` a la generación propietaria.
- La UI ya no ejecuta `request_shutdown("update")` al iniciar una actualización: solo valida `update_runner.py`, lanza el supervisor con `--parent-pid`, informa el inicio y permanece activa hasta recibir el comando del supervisor. Si `Popen()` falla, Nova permanece abierta y muestra el error.
- `update_runner.py` es la única autoridad que envía `shutdown_for_update`; `RuntimeLifecycleManager` conserva la traducción de ese comando a `request_shutdown("update")`, sin sleeps ni cierres temporizados desde la UI.
- El updater captura el proceso propietario antes de solicitar cierre. En Windows usa HANDLE real y firmas ctypes explícitas para `OpenProcess`, `GetProcessTimes`, `WaitForSingleObject` y `CloseHandle`.
- Una actualización solo queda autorizada cuando el proceso propietario terminó realmente y el updater conserva el lock de la sesión como guard exclusivo.
- La coordinación convierte errores operacionales normales de componentes, metadata, lock, mailbox, captura de proceso y publicación del guard en fallos estructurados. Antes de una terminación verificada conserva el runtime original y no relanza; después de una terminación verificada nunca actualiza sin guard, libera cualquier guard retenido best-effort y hace un único relanzamiento visible de recuperación con código `4`.
- El guard se mantiene durante descarga, staging, reemplazo, instalación de dependencias, validación y rollback; si otro runtime gana una carrera por el lock, la actualización falla cerrada sin modificar archivos.
- Una vez que `coordinate_runtime_shutdown()` devuelve `ok=True`, el supervisor garantiza exactamente un intento de relanzamiento visible con `--post-update`, incluso si `run_update()`, la lectura posterior de versión, `write_status()`, el logging o la liberación del guard producen errores.
- El orden de recuperación queda fijado como actualización/rollback → liberación del guard → `launch_nova()`. Las operaciones administrativas de estado/log son best-effort y no pueden impedir el intento de recuperación.
- Si la coordinación falla antes de verificar la terminación, no se ejecuta el updater ni se lanza otra instancia; la escritura de estado es best-effort y se intenta `_show_surviving_runtime()` cuando existe una identidad válida.
- `pip install -r requirements.txt` tiene timeout explícito de 15 minutos por defecto, inyectable para pruebas; valores no positivos/no finitos se rechazan y valores superiores a una hora se limitan. El timeout afecta solo al proceso directo de pip, que se termina y espera antes de iniciar recuperación.
- Un `TimeoutExpired` de pip se considera mutación de dependencias iniciada: ejecuta rollback de archivos, conserva el backup, persiste el detalle en `data/update_recovery.json` y marca `dependencies_may_have_changed=true` y `recovery_required=true`, con `files_rollback_ok` según el resultado real del rollback.
- El rollback usa un manifiesto transaccional explícito con `modified_existing`, `deleted_existing`, `created_new` y `unchanged`: restaura los dos primeros, elimina únicamente `created_new` y nunca toca `unchanged`.
- `managed_files.json` conserva y restaura su estado previo. Reemplazos viables usan archivo temporal + `os.replace()` para reducir instalaciones parciales.
- El rollback transaccional garantiza el estado de los archivos administrados, no una reversión exacta de `.venv`. Si `pip` llegó a iniciarse, el entorno de dependencias puede haber cambiado aunque los archivos se restauren correctamente.
- En una recuperación posterior a pip se conservan el backup y un estado explícito con `files_rollback_ok`, `dependencies_may_have_changed` y `recovery_required`; el mensaje no afirma que volver a ejecutar `requirements.txt` elimine paquetes adicionales o reconstruya exactamente el entorno anterior.
- Si el rollback de archivos no puede completarse, el error se propaga, se escribe estado explícito de recuperación y el backup se conserva para reparación manual.
- Botón UI, `ACTUALIZAR_NOVA.cmd` y ejecución interactiva directa de `nova_updater.py` usan el mismo supervisor. El camino interno `--yes` solo se utiliza después de la coordinación segura.
- El autostart sigue bajo HKCU, sin administrador y desactivado por defecto. Al desactivar solo elimina `NovaDesktop` si el valor coincide exactamente con la instalación actual; una entrada de otra instalación produce conflicto explícito y no se modifica.
- `WM_QUERYENDSESSION`/`WM_ENDSESSION` se integran al shutdown real para logoff/apagado de Windows.
- Nova Doctor informa lifecycle, ventana visible/oculta, bandeja lista/degradada, propiedad/scope de instancia, estado real/conflicto de autostart, último motivo de salida y errores recientes sin contenido sensible.
- Se añaden pruebas de bandeja con visibilidad real, timeout y callback tardío; rollback con archivos reales y SHA-256; timeout recuperable de pip sin esperas reales; incertidumbre de dependencias posterior a pip; relanzamiento ante excepciones administrativas y de coordinación; PID reutilizado/metadata obsoleta; updater activo; carrera de guard; cleanup con scheduler Tk roto; y la integración multiproceso real de Windows.
- CI mantiene la suite completa Ubuntu y ejecuta explícitamente en `windows-latest` lifecycle, bandeja, rollback, IPC/procesos, updater, session shutdown, Gaming Awareness, Instant Wake/hotkeys y núcleo nativo.

## v0.9.8 — Gaming Reliability

- Gaming Awareness valida identidad/frescura del proceso y sincroniza su estado mediante eventos Tk-safe.
- Wallpaper Engine queda excluido como falso positivo incluso ante configuraciones antiguas.
- Launchers/helpers/updaters no mantienen Gaming Mode por sí solos.
- La restauración de Qwen queda protegida contra reentradas y carreras de temporizadores.
- Se añade puerta específica de confiabilidad en `windows-latest` además de la suite completa Ubuntu.

## v0.7.4 — Event-driven Vision

- Nueva capa `EventDrivenVision` local que usa capturas únicamente bajo una consulta visual explícita o un evento configurado; no existe hilo de screenshots periódicos.
- Captura preferentemente la última aplicación externa observada por Perception Engine para evitar fotografiar la propia ventana de Nova cuando el usuario abre el asistente.
- Análisis local mediante Ollama `/api/chat` con imágenes y comprobación previa de capability `vision`; no descarga modelos automáticamente ni usa OpenAI como fallback automático.
- Por defecto solo `crash_signal` puede disparar una captura automática. Las anomalías de CPU/RAM no abren visión salvo configuración explícita, porque normalmente se explican mejor con métricas.
- Rate limit de capturas automáticas: cooldown y máximo por hora para impedir loops de screenshots ante eventos repetidos.
- Las imágenes se procesan en memoria y no se conservan por defecto (`retain_images=false`). El texto del análisis tampoco se persiste por defecto (`persist_analysis=false`).
- La base `data/vision_events.db` guarda únicamente metadatos seguros de ejecución, categoría y confianza; no guarda imágenes, prompts ni títulos de ventana por defecto.
- Todo contenido de pantalla se trata como dato externo no confiable: instrucciones visibles en webs, terminales, chats, juegos o documentos nunca autorizan acciones.
- El prompt visual prohíbe transcribir contraseñas, tokens, cookies, claves API u otros secretos visibles.
- Nuevas herramientas: `vision_status`, `vision_describe_screen`, `vision_last`, `vision_recent_events`.
- Routing directo para `¿qué ves en mi pantalla?`, `mira mi pantalla`, estado de visión y último análisis visual.
- Event-driven Vision se enlaza a Anomaly Detection mediante callback en memoria; no necesita polling visual.
- Se añade `anomaly_detection.py` como módulo de compatibilidad estable para corregir la diferencia de nombre publicada en v0.7.3 (`anomaly.py` vs. adaptadores `anomaly_detection`).
- Nuevas pruebas de privacidad, routing, ausencia de polling, política de triggers, rate limiting, capability del modelo y compatibilidad de Anomaly Detection.

## v0.7.3 — Anomaly Detection

- Nuevo `AnomalyDetector` local y determinista sobre Perception Engine + Context Intelligence.
- Aprende una línea base de CPU/RAM por actividad (`gaming`, `programming`, `browsing`, etc.) y una línea base de consumo por proceso.
- Las alertas requieren desviaciones sostenidas para reducir falsos positivos por picos breves.
- Contexto sensible: jugar o usar procesos pesados esperados como Ollama eleva los umbrales antes de considerar el consumo anómalo.
- Detecta procesos nuevos con consumo alto o procesos conocidos que se desvían de su comportamiento normal.
- Señales best-effort de crash mediante apariciones de Windows Error Reporting (`WerFault.exe`/`wermgr.exe`), con escalado si se repiten en una ventana corta.
- Nueva base local `data/anomaly_detection.db`; no guarda cmdline, rutas, títulos de ventana, contenido de pantalla, teclado ni portapapeles.
- Nunca mata, bloquea, desinstala o repara procesos automáticamente.
- Herramientas: `anomaly_status`, `anomaly_recent`, `anomaly_mark_process_expected`, `anomaly_acknowledge`.
- Routing directo para consultas como `¿hay algo raro en mi PC?`, estado del baseline y marcado explícito de procesos esperados.
- El Agent recibe solo un resumen compacto de anomalías pendientes y trata los nombres de proceso como datos no confiables.
- Pruebas para baseline, carga alta esperada en juegos, proceso nuevo pesado, preferencias del usuario, señales repetidas de crash, privacidad y routing.

## v0.7.2 — Workspace Auto-Detection

- Aprendizaje local persistente de asociaciones aplicación ↔ workspace en `data/workspace_autodetect.db`.
- Solo entrena con evidencia confiable acumulada (`cwd` dentro del workspace o coincidencia corroborada por el workspace activo); un título de ventana aislado nunca entrena.
- Las asociaciones contradictorias pierden confianza y las fijadas explícitamente por el usuario tienen prioridad.
- Detección de ambigüedad para evitar elegir un proyecto cuando dos asociaciones tienen confianza parecida.
- Activación automática del workspace permanece deshabilitada por defecto y exige umbrales estrictos si el usuario decide habilitarla.
- Herramientas para estado, asociaciones, aprendizaje explícito y olvido de asociaciones.
- Routing directo para preguntar qué proyecto cree Nova que está activo.

## v0.7.1 — Context Intelligence

- Nueva capa determinista sobre Perception Engine para inferir actividad probable y puntuar relevancia contextual.
- Reduce rebotes repetitivos entre aplicaciones y evita llenar el prompt con cambios irrelevantes.
- Infiere actividades como programación, investigación, navegación, juegos, ofimática y administración de archivos.
- El prompt recibe un bloque compacto de aplicación, actividad, workspace probable, relevancia y solo señales importantes.
- Los títulos de ventana quedan fuera del prompt por defecto y siguen tratándose como datos no confiables.
- Herramientas: `context_activity`, `context_relevant_recent`, `context_intelligence_status`.

## v0.7.0 — Perception Engine

- Nuevo `PerceptionEngine` nativo y local que observa metadatos de la ventana/proceso activo y carga básica del sistema sin usar LLM.
- Sondeo ligero por defecto cada 1100 ms; no realiza screenshots periódicos, no captura teclado y no lee portapapeles.
- Cuando Nova pasa al primer plano conserva la última ventana externa observada, permitiendo responder qué aplicación/ventana se estaba usando antes de abrir el asistente.
- Clasificación barata de contexto: editor de código, navegador, terminal, explorador, juego, comunicación, multimedia, Office y otros.
- Inferencia de workspace probable usando cwd del proceso y nombre/carpeta del proyecto en el título de ventana; nunca cambia automáticamente el workspace por defecto.
- Contexto de percepción inyectado al agente con una barrera explícita contra prompt injection: los títulos de ventana se consideran datos no confiables.
- Historial local limitado en `data/perception.db` para cambios de aplicación, workspace probable y presión/recuperación de CPU/RAM.
- Los títulos de ventana no se persisten por defecto (`persist_window_titles=false`).
- Nuevas herramientas de solo lectura: `perception_context`, `perception_status` y `perception_recent`.
- Routing directo para preguntas como `¿qué aplicación tengo abierta?`, `¿qué estaba usando antes de abrir Nova?`, `¿está funcionando tu percepción?` y consultas de cambios recientes.
- Nova Doctor incorpora una comprobación específica de Perception Engine y sus garantías de privacidad.
- La UI inicia y detiene el motor junto con Nova; el hilo de percepción es daemon y no bloquea el cierre/actualizador.
- Nuevas pruebas para detección de workspace, preservación de la última ventana externa, privacidad de eventos, clasificación y routing.

## v0.6.7 — Core Consolidation

- `app.py` pasa a usar un único bootstrap estable: `assistant.core_runtime.install_core_runtime()`.
- Se elimina la cadena de runtimes versionados `v060_runtime` → `v066_runtime`.
- Los adaptadores de Agent/Tools/UI se renombran por dominio (`*_workspace`, `*_semantic`, `*_continuity`, `*_diagnostics`) en lugar de por número de versión.
- `MemoryStore` integra nativamente Workspaces, búsqueda léxica/híbrida, Semantic Memory, asociación de tareas y Continuity Engine; ya no necesita monkey patches de memoria.
- `NovaDoctor` integra nativamente Semantic Memory, Self Repair, Performance Profiler y una comprobación del contrato de arquitectura.
- Se eliminan los adaptadores versionados de memoria y Doctor ya absorbidos por el núcleo.
- El updater de GitHub eliminará automáticamente los archivos `v06x_*` administrados que ya no formen parte de la Release, después de crear su backup habitual.
- Los módulos históricos locales `agent.py`, `tools.py`, `ui.py` y `task_engine.py` se conservan como contrato legacy hasta su migración completa; 0.6.7 los extiende mediante adaptadores estables.
- Nuevas pruebas impiden reintroducir archivos `v0*.py`, verifican el bootstrap único y confirman que las capacidades de memoria existen sin instaladores.

## v0.6.6 — Self Repair + Performance Profiler

- Nova Doctor pasa de diagnóstico a diagnóstico + reparación determinista con confirmación explícita antes de instalaciones, descargas o cambios importantes.
- Reparaciones disponibles para archivos gestionados de Nova, dependencias Python, Ollama, modelos principal/semántico, GitHub CLI/autenticación y fallback de Playwright.
- Nueva ventana de Nova Doctor con botones de reparación, re-diagnóstico automático y resumen de rendimiento.
- Nuevo Performance Profiler 100% local en `data/performance.db`; no registra prompts, mensajes, secretos ni contenido de archivos.
- Métricas de duración para Agent, memoria, embeddings, herramientas, Task Engine (cuando está disponible) y Nova Doctor.
- Nuevas herramientas `performance_summary`, `performance_recent` y `doctor_repairs`.
- Routing directo para preguntas como `¿cómo va tu rendimiento?`, `¿por qué estás lento?` y `¿qué puedes reparar?`.
- El profiler identifica cuellos de botella por promedio y conserva un historial acotado para evitar crecimiento indefinido.
- El atajo global predeterminado cambia de `Ctrl + Espacio` a `Ctrl + Alt + Espacio` para reducir conflictos con juegos.
- Las instalaciones existentes que todavía usan exactamente el antiguo `Ctrl + Espacio` se migran automáticamente; cualquier hotkey personalizado distinto se conserva.
- Nuevas pruebas para profiler, privacidad de metadatos, detección de reparaciones, routing y migración del hotkey.

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