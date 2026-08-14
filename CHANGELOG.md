# Changelog

## v0.9.9 — Resident Mode & Runtime Lifecycle

- Añade `RuntimeLifecycleManager` nativo con estados `starting`, `running`, `hidden`, `shutting_down` y `stopped`, separando ocultar la ventana de terminar el proceso.
- El botón X usa `withdraw()` únicamente cuando Resident Mode está habilitado y la bandeja confirmó que está operativa; ocultar no detiene hotkeys, wake word, F9, Perception, Gaming Awareness ni descarga Qwen.
- Si la bandeja falla, no confirma readiness o expira su timeout, Nova entra en modo degradado, permanece visible y X vuelve a cerrar normalmente. `--background` nunca deja un proceso invisible sin icono recuperable.
- `TrayController` usa `pystray` sin bloquear Tk y solo marca `available=True` después del callback de inicialización del backend; incluye estados de Qwen/Gaming, controles de Qwen, update, autostart y salida real.
- El cierre real es idempotente y ejecuta una secuencia controlada. Los errores de un componente se registran sin impedir los pasos restantes.
- El lock físico de instancia ya no se libera durante el lifecycle: permanece adquirido hasta que `app.py` sale de `mainloop()` y alcanza su finalizador de proceso.
- La instancia única queda limitada a usuario + sesión de Windows. El scope se deriva del SID del usuario, persistiendo solo su hash, y Windows Session ID.
- `owner.json` se escribe atómicamente y registra PID, `owner_id`/generación aleatoria, rol (`runtime`/`updater`), scope, hash de usuario, Session ID y timestamp. El lock del kernel sigue siendo la fuente de exclusión.
- El IPC residente pasa a archivos independientes por `command_id`, con `target_owner_id`, `command` y `created_at`. Solo acepta `show`, `shutdown_for_update` y `status`; no abre puertos ni servidores de red.
- Una generación nueva descarta comandos malformados, vencidos o dirigidos a otro `owner_id`; un `shutdown_for_update` abandonado por un crash nunca se ejecuta contra el runtime siguiente.
- Dos emisores concurrentes no se sobrescriben porque cada orden usa un archivo atómico independiente.
- Una segunda ejecución no construye Agent/Tk/servicios y solo devuelve éxito si pudo entregar `show` a la generación propietaria.
- El updater captura el proceso propietario antes de solicitar cierre. En Windows usa HANDLE real y firmas ctypes explícitas para `OpenProcess`, `WaitForSingleObject` y `CloseHandle`.
- Una actualización solo queda autorizada cuando el proceso propietario terminó realmente y el updater adquirió el lock de la sesión.
- Tras confirmar la muerte del runtime, `update_runner.py` adquiere el lock como rol `updater` y mantiene ese guard durante descarga, staging, reemplazo, dependencias, validación y rollback.
- Si no se puede capturar/esperar el proceso, entregar `shutdown_for_update` o adquirir el guard dentro del timeout, la actualización falla cerrada sin modificar archivos.
- Botón UI, `ACTUALIZAR_NOVA.cmd` y ejecución interactiva directa de `nova_updater.py` usan el mismo supervisor. El camino interno `--yes` solo se utiliza después de la coordinación segura.
- Después de éxito o error del updater, una vez terminado el runtime anterior de forma verificable, se libera el guard y se lanza exactamente una instancia visible con `--post-update`.
- El autostart sigue bajo HKCU, sin administrador y desactivado por defecto. Al desactivar solo elimina `NovaDesktop` si el valor coincide exactamente con la instalación actual; una entrada de otra instalación produce conflicto explícito y no se modifica.
- `WM_QUERYENDSESSION`/`WM_ENDSESSION` se integran al shutdown real para logoff/apagado de Windows.
- Nova Doctor informa lifecycle, ventana visible/oculta, bandeja lista/degradada, propiedad/scope de instancia, estado real/conflicto de autostart, último motivo de salida y errores recientes sin contenido sensible.
- Se añaden pruebas de bandeja asíncrona, owner targeting, autostart ajeno, updater fail-safe y una integración multiproceso real en Windows para lock, segunda ejecución, comandos concurrentes, terminación verificada, guard del updater y recuperación tras crash.
- CI mantiene la suite completa Ubuntu y ejecuta explícitamente en `windows-latest` lifecycle, IPC/procesos, updater, session shutdown, Gaming Awareness, Instant Wake/hotkeys y núcleo nativo.

## v0.9.8 — Gaming Reliability

- Gaming Awareness valida identidad/frescura del proceso y sincroniza su estado mediante eventos Tk-safe.
- Wallpaper Engine queda excluido como falso positivo incluso ante configuraciones antiguas.
- Launchers/helpers/updaters no mantienen Gaming Mode por sí solos.
- La restauración de Qwen queda protegida contra reentradas y carreras de temporizadores.
- Se añade puerta específica de confiabilidad en `windows-latest` además de la suite completa Ubuntu.

## Historial anterior

Las versiones anteriores permanecen documentadas en sus Releases/tags y documentación técnica del repositorio, incluyendo Event-driven Vision, Anomaly Detection, Workspace Auto-Detection, Context Intelligence, Perception Engine, Core Consolidation, Self Repair, Continuity, Semantic Memory y el updater GitHub-native.
