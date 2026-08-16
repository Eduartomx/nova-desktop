# Changelog

## v0.10.0 — Safe Actions & Repository Intelligence

- Añade un Action Broker local como autoridad única antes de ejecutar herramientas: cada solicitud queda ligada a herramienta, hash SHA-256 de argumentos, propietario, scope, sesión, tarea y snapshot revalidable.
- Clasifica todas las herramientas en lectura, lectura sensible, reversible, mutación, alto riesgo o prohibida; las herramientas desconocidas fallan cerradas.
- Incorpora perfiles `safe`, `balanced` y `trusted`; `balanced` es el valor inicial para instalaciones nuevas y la migración conserva o endurece las confirmaciones antiguas.
- Las acciones de alto riesgo solo aceptan permiso de una vez. Los grants de tarea no cruzan tarea, owner, scope ni herramienta.
- La tarjeta de aprobación vive exclusivamente en la UI local de Tk. Cerrar la tarjeta equivale a denegar; timeout, cancelación, shutdown o update liberan la espera.
- `browser_fill(..., submit=true)` pide permiso antes de rellenar. Enter, UI Automation y mouse no pueden usarse como bypass del envío sensible.
- `write_file` solicita permiso antes de crear carpetas, copiar backup o escribir, y conserva los backups previos a sobrescritura.
- PowerShell aplica un denylist fail-closed para borrado masivo, formato, arranque, seguridad, protecciones y credenciales; el audit log nunca guarda comandos, texto, secretos ni argumentos completos.
- Task Engine reconoce `waiting_for_approval`, `approved`, `denied` y `expired`; la espera humana no consume timeout ni provoca retry/replan y las aprobaciones se consumen exactamente una vez.
- Añade botón visible y acción de bandeja para detener automatización; el hotkey de emergencia es configurable pero queda vacío por defecto.
- Añade Repository Intelligence para responder versión, novedades, disponibilidad de actualización, actividad y archivos públicos del repositorio propio configurado.
- El cliente GitHub read-only usa exclusivamente `api.github.com`, `urllib`, timeout, ETag, límites de tamaño y cache pública atómica; no usa `gh`, tokens, cookies ni autenticación.
- Versiones y changelog funcionan offline con archivos locales, `update_last.json` y cache. Las respuestas siempre indican la evidencia utilizada.
- Repositorio, releases, commits, issues y PR se tratan como datos externos no confiables y nunca pueden autorizar herramientas ni ejecutar instrucciones encontradas.
- Añade routing determinista anterior al LLM para consultas de versión, changelog, actualización y estado del repositorio.
- Minecraft Agent y Hexabot permanecen explícitamente fuera de alcance de v0.10.0.

## v0.9.9 — Resident Mode & Runtime Lifecycle

- Añade `RuntimeLifecycleManager` con estados `starting`, `running`, `hidden`, `shutting_down` y `stopped`; ocultar la ventana ya no equivale a terminar Nova.
- X usa `withdraw()` solo con bandeja confirmada; una bandeja degradada nunca debe dejar Nova invisible sin mecanismo de recuperación.
- El lock físico de instancia permanece adquirido hasta salir de `mainloop()`; `owner.json` identifica generaciones con PID + creation time + `owner_id` y el scope Windows usa usuario + Session ID sin persistir el SID en claro.
- IPC residente usa archivos atómicos dirigidos a `target_owner_id`; solo acepta `show`, `shutdown_for_update` y `status`.
- Añade `update_supervisor.lock` y fija el orden validado `supervisor mutex → runtime/recovery guard → motor/recovery deja estado validado → helper estable → release guard → CAS a cleared → helper relanza → release supervisor mutex`.
- Un segundo supervisor devuelve código `5` sin cerrar Nova, tocar `update_last.json`, ejecutar updater/pip ni relanzar.
- La UI evita doble clic, conserva el `Popen` del supervisor y usa `root.after()` + `poll()` sin `wait()` en Tk.
- La UI elimina `CREATE_NEW_CONSOLE`: el supervisor usa `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP` y redirige su salida temprana a `data/updater_logs` sin abrir consola o pestaña visible.
- `process_launch.py` centraliza la selección de Python de consola/GUI sin resolver el venv al intérprete base; los dos caminos de relanzamiento final prefieren `pythonw.exe` del mismo entorno y conservan fallback oculto.
- `update_runner.py` sigue siendo la única autoridad que solicita `shutdown_for_update` y ejecuta el motor interno **en el mismo proceso** mientras conserva supervisor mutex + runtime guard.
- `--yes` deja de ser autorización interna. `resident_update_engine.py` ejecutado directamente devuelve error seguro antes de GitHub/staging/mutación/pip.
- `nova_updater_legacy.py` queda bloqueado como entrypoint directo antes de sus side effects; el código legacy permanece solo como implementación importable de compatibilidad.
- El backup se crea y valida **antes** de toda mutación y, acto seguido, se publica de forma durable `update_recovery.json` con `schema_version=2` y estado `transaction_prepared`.
- No se reemplaza/elimina ningún archivo, no se modifica `managed_files.json` y no se inicia pip antes de que exista ese journal durable.
- `files_may_have_changed=true` se publica antes del primer reemplazo/eliminación; `dependencies_may_have_changed=true` se publica en `dependencies_starting` antes de permitir que pip ejecute código.
- El supervisor inspecciona el journal tras cualquier salida del motor. Un rc inesperado con intento activo/corrupto devuelve `7` y **no relanza Nova**; la decisión ya no depende únicamente del código de retorno.
- El motor no ejecuta el clear terminal: un update correcto termina en `update_validated` y un rollback correcto en `rollback_validation_completed`; el supervisor/recovery coordinator realiza el handoff final.
- El journal schema 2 exige `attempt_id`, `generation` monotónica y un grafo explícito de transiciones.
- Estados schema 2: `transaction_prepared`, `files_applying`, `files_applied`, `dependencies_starting`, `dependencies_running`, `update_validation_in_progress`, `update_validated`, `pip_termination_unconfirmed`, `waiting_for_processes`, `rollback_in_progress`, `rollback_completed`, `rollback_validation_in_progress`, `rollback_validation_completed`, `dependency_repair_required` y `cleared`.
- `cleared` es terminal y no admite transiciones salientes, ni siquiera `cleared → cleared`; ningún fallo posterior de launch puede reabrir el journal.
- Cada transición obtiene un lock del SO, relee el journal y aplica compare-and-swap sobre `attempt_id + generation`; escritores obsoletos no pueden sobrescribir otro intento ni estados más avanzados.
- Journals schema 1 conocidos se migran explícitamente; JSON truncado, schema/estado desconocido o migración no demostrable fallan cerrados.
- Las escrituras de journal usan temporal + `flush` + `fsync` cuando corresponde + `os.replace`; errores persistidos se sanitizan.
- El restaurador de archivos se separa completamente de la publicación del journal: `rollback_in_progress` se publica **antes** de restaurar y el restaurador puro nunca borra ni sustituye el estado activo.
- Rollback idempotente restaura modificados/eliminados, elimina únicamente `created_new`, restaura `managed_files.json` y revalida traversal/symlink/rutas autorizadas en cada reanudación.
- Un crash a mitad de rollback puede reanudarse repitiendo la restauración; el motor deja el estado en `rollback_validation_completed` y el clear solo se autoriza durante el handoff final.
- En Windows pip usa Job Object autoritativo con `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`: `CreateProcessW(CREATE_SUSPENDED) → AssignProcessToJobObject → ResumeThread`.
- Si el Job no puede establecerse mientras pip sigue suspendido, pip no se considera iniciado y no existe fallback autoritativo a `psutil`.
- Las estructuras, constantes, firmas ctypes y handles de Job/process/thread se gestionan explícitamente; jobs anidados se tratan de forma fail-closed.
- Identidades pendientes usan PID + creation time + rol. Un PID reutilizado con otro creation time no bloquea ni se termina como el proceso original.
- Código `6` representa `pip_termination_unconfirmed`: no rollback concurrente, no launch y cuarentena persistente conservando backup/journal.
- Código `7` representa recovery requerido/en curso/no verificable; bloquea startup y nuevos updates hasta resolver el intento durable.
- Antes de modificar un `requirements.txt`, Nova captura dentro del backup un snapshot exacto de distribuciones normalizadas + versiones, conjunto instalado, SHA-256 del snapshot y del requirements anterior e imports críticos aplicables.
- Si pip nunca ejecutó código, recovery no exige una reparación innecesaria de dependencias. Si pudo ejecutar, se detectan paquetes añadidos/eliminados, versiones diferentes, snapshot/hash corrupto, requirements restaurado con entorno distinto y fallo de import crítico.
- Los imports críticos se prueban en subprocess aislado/acotado con `python -I`; recovery nunca ejecuta pip para instalar/desinstalar/degradar.
- Una discrepancia de dependencias transiciona a `dependency_repair_required`, conserva cuarentena + backup y bloquea el relanzamiento.
- El snapshot valida compatibilidad, pero **no implementa rollback bit-a-bit de `.venv`** y no convierte el `requirements.txt` no fijado en un lockfile reproducible.
- Antes de aplicar archivos se prepara `data/recovery_runtime/`: generaciones stdlib-only inmutables con manifest y SHA-256; `active.json` se reemplaza solo después de verificar la generación nueva, conservando la copia buena anterior.
- El bundle estable incluye `process_launch.py` y `recovery_handoff.py`; `app.py` exige el conjunto exacto de archivos/hash y una alteración invalida la generación completa.
- Si el proceso muere entre `transaction_prepared` y la creación del bundle estable, recovery solo puede reconstruirlo automáticamente mientras `files_may_have_changed=false`, cuando el árbol administrado todavía es el previo a la actualización.
- `app.py` ejecuta recovery antes de `_claim_instance`, Tk, core, Agent y UI. Si el bootstrap administrado falla con journal activo, intenta la generación estable validada por hash.
- Si bootstrap administrado y estable fallan, Windows muestra `MessageBoxW` desde `app.py` y sale con `7`; `pythonw.exe` no depende de stderr como único canal.
- `recovery_handoff.py` es la implementación compartida para update y recovery: verifica el journal exacto, valida el bootstrap estable, inicia el helper con el intento todavía activo, libera el runtime guard y solo entonces ejecuta el CAS `validated → cleared`.
- El helper exige `attempt_id`, generation, estado de origen, modo y token exactos. Mientras el journal sigue validado espera; otro intento, otro writer, corrupción o timeout impiden el lanzamiento.
- Una muerte real del supervisor después del spawn pero antes del CAS conserva cuarentena y el helper expira sin lanzar. Una muerte después del CAS permite que el helper independiente relance exactamente una vez.
- Un fallo al iniciar el helper o liberar el guard conserva el estado validado; un CAS obsoleto conserva la generación más reciente y no autoriza launch.
- Si `Popen(app.py)` falla después del `cleared` terminal, se registra en `data/updater_logs/recovery_handoff.log`; el journal no se reabre ni se reescribe.
- Dos recovery supervisors reales no pueden restaurar/validar/relanzar a la vez; locks del kernel + CAS hacen que solo uno avance y el otro devuelva `7`.
- Si se mata al propietario del recovery, el lock del SO se libera y otro proceso reanuda desde el journal durable.
- Añade pruebas subprocess reales que matan el motor con `os._exit` después del journal, primer archivo, mitad de apply, `files_applied`, antes de pip, antes/mitad/después de rollback/validation y antes del handoff.
- Añade crash tests reales del handoff después del spawn y después del CAS, además de checks de guard-before-clear, token/generation exactos, bootstrap estable alterado, CAS stale y ausencia de doble launch/double-release.
- Windows CI mata además al updater mientras un proceso está contenido por el Job Object y exige que ningún hijo sobreviva; esa prueba nativa falla CI si se reporta como skipped.
- Añade pruebas de snapshot de dependencias, entrypoints directos inertes, bootstrap estable/tamper, CAS stale-writer, recovery multiproceso real y reanudación tras muerte del dueño del lock.
- CI ejecuta primero un gate focal de handoff/terminal/update-runner/recovery/crash/multiprocess tanto en Ubuntu como Windows, seguido de las suites completas y las carreras Job Object/recovery repetidas.
- Nova Doctor conserva diagnóstico del supervisor por mutex de kernel, último resultado y recovery/quarantine pendiente; no se exponen comandos completos, tokens, prompts ni contenido de archivos.
- Autostart continúa bajo HKCU, sin administrador y desactivado por defecto; no elimina una entrada perteneciente a otra instalación.
- `WM_QUERYENDSESSION`/`WM_ENDSESSION` continúan integrados al shutdown real para logoff/apagado de Windows.

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
