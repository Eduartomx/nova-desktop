# Nova Desktop

Nova es un asistente virtual local para Windows con Ollama, herramientas de escritorio/navegador, memoria y workspaces, Perception, Gaming Awareness, Skills, voz local y updater GitHub-native.

GitHub (`Eduartomx/nova-desktop`) es la fuente de verdad. El updater estable sincroniza código desde la Release/tag publicada, valida Git blob SHA y conserva backup/rollback; no depende de ZIPs.

## Estado

La base publicada es **v0.9.8 — Gaming Reliability**. La rama de desarrollo prepara **v0.9.9 — Resident Mode & Runtime Lifecycle**; no debe publicarse hasta validación manual y aprobación.

### v0.9.8 — Gaming Reliability

- valida identidad/frescura del juego;
- excluye Wallpaper Engine como falso positivo;
- launchers/helpers/updaters no mantienen Gaming Mode;
- sincroniza UI mediante eventos Tk-safe;
- protege la restauración de Qwen frente a carreras;
- añade validación específica en Windows CI.

### v0.9.9 — Resident Mode & Runtime Lifecycle

Resident Mode separa **ocultar la ventana** de **terminar Nova**. Con una bandeja confirmada como operativa, X usa `withdraw()` y Nova continúa con hotkeys, wake word, F9, Perception, Gaming Awareness y política de Qwen activas.

Si la bandeja falla o no confirma su inicialización, Nova queda visible y X vuelve a cerrar normalmente. `--background` nunca debe dejar un proceso invisible sin icono operativo.

El cierre real es idempotente. `RuntimeLifecycleManager` bloquea trabajo nuevo, detiene voz/wake, Gaming/Perception, Browser Agent, guarda estado, aplica `unload_on_exit`, detiene bandeja y hook de sesión y destruye Tk. El lock físico de instancia permanece adquirido durante esa limpieza y `app.py` lo libera únicamente al salir de `mainloop()`.

## Una sola instancia por usuario/sesión

Antes de cargar Agent/Tk/servicios, Nova adquiere un lock del kernel. En Windows el scope usa el SID del usuario **solo en memoria**, guarda únicamente su hash y lo combina con Windows Session ID.

```text
%LOCALAPPDATA%\Nova\runtime\scope-<scope_id>\
  runtime.lock
  owner.json
  commands\
```

`owner.json` registra PID, tiempo real de creación del proceso, `owner_id`/generación aleatoria, rol (`runtime` o `updater`), scope, hash de usuario, Session ID y timestamp. El lock del kernel, no el PID, es la fuente de exclusión.

Una segunda ejecución no construye Agent/Tk/servicios: envía `show` al `owner_id` actual y solo devuelve éxito si pudo entregar la orden.

## IPC residente

No se abren puertos ni servidores. Cada orden es un archivo atómico independiente con `command_id`, `target_owner_id`, `command` y `created_at`. Solo se aceptan `show`, `shutdown_for_update` y `status`.

Mensajes malformados, vencidos o dirigidos a otra generación se eliminan sin ejecutarse. Archivos separados evitan que emisores concurrentes se sobrescriban.

## Bandeja

Nova usa `pystray`. `available=True` solo se establece después del callback de inicialización del backend, que hace visible el icono y confirma esa visibilidad. Excepción, icono no visible o timeout producen estado degradado.

## Inicio con Windows

Está desactivado por defecto y usa `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\NovaDesktop`. El comando usa la instalación real con `--background` y quoting seguro. Al desactivar, Nova elimina la entrada solo si coincide con esta instalación; si pertenece a otra instalación informa conflicto y no la modifica.

## Updater seguro

La actualización requiere simultáneamente confirmar que el proceso propietario terminó realmente y adquirir el lock de la sesión como guard del updater. `update_runner.py` captura el proceso antes de pedir `shutdown_for_update`; en Windows conserva un HANDLE y espera su terminación con APIs Win32 tipadas.

La UI no inicia un cierre local al pulsar **Actualizar**. Únicamente valida `update_runner.py`, inicia el supervisor con `--parent-pid` y permanece activa. El supervisor es la única autoridad que envía `shutdown_for_update`; ese comando sigue llegando a `RuntimeLifecycleManager`, que lo traduce a `request_shutdown("update")`. Si `Popen()` falla, Nova permanece abierta y muestra el error.

La coordinación del supervisor es exception-safe para errores operacionales. Si falla antes de confirmar la terminación del runtime, no se ejecuta el updater, no se lanza otra instancia y se intenta restaurar/mostrar la existente. Si el runtime ya terminó de forma verificable pero falla la adquisición/publicación del guard, tampoco se ejecuta el updater: se libera cualquier guard retenido como best-effort y se intenta exactamente un relanzamiento visible de recuperación con código `4`.

Después de una coordinación correcta adquiere el lock como rol `updater` y mantiene ese guard durante staging, reemplazo, dependencias, validación y rollback. Si no puede confirmar proceso + guard dentro del timeout, no modifica archivos.

Una vez que la coordinación devuelve `ok=True`, el supervisor garantiza un único intento visible de relanzamiento con `--post-update`. El orden es: actualización/rollback → liberación del guard → `launch_nova()`. Lecturas de versión, escritura de estado, logging e incluso un error al liberar el guard se tratan como best-effort y no pueden saltarse ese intento de recuperación.

El rollback transaccional cubre **archivos administrados**: restaura archivos modificados/eliminados, elimina solo archivos creados por la actualización, conserva los unchanged y restaura `managed_files.json`. Sin embargo, si `requirements.txt` cambió y `pip` llegó a iniciarse, no existe una garantía equivalente para el estado exacto de `.venv`.

`pip install -r requirements.txt` tiene un timeout explícito de **15 minutos** por defecto; el valor es inyectable para pruebas, los valores no positivos se rechazan y los excesivos se limitan a una hora. Si expira, Nova termina y espera el proceso directo de pip, activa el rollback de archivos y marca `dependencies_may_have_changed=true` y `recovery_required=true`. Un timeout de pip no se implementa matando externamente todo `nova_updater.py`, por lo que la transacción conserva la oportunidad de recuperar archivos antes de que el supervisor libere el guard y relance Nova.

Si una actualización falla después de iniciar pip, Nova restaura los archivos cuando es posible y conserva el backup, pero persiste un estado de recuperación con `files_rollback_ok`, `dependencies_may_have_changed` y `recovery_required`. El detalle del timeout/error queda en `data/update_recovery.json`. No se afirma que volver a ejecutar `pip install -r requirements.txt` elimine paquetes adicionales ni reconstruya exactamente el entorno anterior.

UI, `ACTUALIZAR_NOVA.cmd` y ejecución interactiva directa usan el mismo supervisor.

## Atajos

Defaults actuales: **Ctrl + Alt + N**, **Ctrl + Alt + Shift + N** y **F9**. Resident Mode conserva hotkeys personalizados existentes.

## Configuración

```json
"resident_mode": {
  "enabled": true,
  "close_to_tray": true,
  "start_with_windows": false,
  "start_hidden": false,
  "notifications": true
}
```

## Nova Doctor

Doctor informa lifecycle, ventana visible/oculta, bandeja lista/degradada, instancia única y scope, autostart real/conflicto, último motivo de salida y errores recientes sin contenido sensible.

## Privacidad

Resident Mode no añade screenshots periódicos, captura de teclado, lectura de memoria de juegos, telemetría externa ni servidores de red. Los archivos de control no guardan prompts, títulos de ventana, contenido de pantalla, tokens ni secretos.

## Pruebas

Ubuntu ejecuta `compileall` y la suite completa. Windows ejecuta explícitamente lifecycle, owner/IPC, una integración con procesos separados reales para lock/comandos/terminación, updater, rollback transaccional, session shutdown, Gaming Awareness, Instant Wake/hotkeys y core.

La documentación técnica completa está en [`docs/v0.9.9-resident-runtime.md`](docs/v0.9.9-resident-runtime.md).

## Validación manual pendiente

Antes de publicar v0.9.9 deben comprobarse en Windows 11: X→bandeja, restauración por hotkey/bandeja, wake/F9 oculto, Gaming Mode + Qwen oculto, segunda ejecución, autostart real, conflicto de otra instalación, actualización real, fallo al iniciar el supervisor sin cerrar Nova, timeout/recuperación de dependencias en un entorno controlado, salida completa y ausencia de servicios duplicados tras reiniciar.