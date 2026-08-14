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
  update_supervisor.lock
  owner.json
  commands\
```

`runtime.lock` protege la instancia residente. `update_supervisor.lock` es un mutex independiente para garantizar **un solo supervisor de actualización por usuario/sesión**. En ambos casos el lock del kernel es la autoridad; PID o metadata no sustituyen la exclusión.

`owner.json` registra PID, tiempo real de creación del proceso, `owner_id`/generación aleatoria, rol (`runtime` o `updater`), scope, hash de usuario, Session ID y timestamp. Una segunda ejecución normal no construye Agent/Tk/servicios: envía `show` al `owner_id` actual y solo devuelve éxito si pudo entregar la orden.

## IPC residente

No se abren puertos ni servidores. Cada orden es un archivo atómico independiente con `command_id`, `target_owner_id`, `command` y `created_at`. Solo se aceptan `show`, `shutdown_for_update` y `status`.

Mensajes malformados, vencidos o dirigidos a otra generación se eliminan sin ejecutarse. Archivos separados evitan que emisores concurrentes se sobrescriban.

## Bandeja

Nova usa `pystray`. `available=True` solo se establece después del callback de inicialización del backend, que hace visible el icono y confirma esa visibilidad. Excepción, icono no visible o timeout producen estado degradado.

## Inicio con Windows

Está desactivado por defecto y usa `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\NovaDesktop`. El comando usa la instalación real con `--background` y quoting seguro. Al desactivar, Nova elimina la entrada solo si coincide con esta instalación; si pertenece a otra instalación informa conflicto y no la modifica.

## Updater seguro

`update_runner.py` adquiere primero `update_supervisor.lock`, antes de leer el runtime o enviar `shutdown_for_update`. El orden obligatorio es:

```text
supervisor mutex
→ coordinación/guard del runtime
→ update o rollback
→ liberación del guard del runtime
→ intento de launch
→ liberación del supervisor mutex
```

El orden inverso no se usa. Si UI, `ACTUALIZAR_NOVA.cmd` o ejecución directa intentan iniciar simultáneamente otro supervisor, solo uno puede poseer el mutex. El segundo no envía `shutdown_for_update`, no ejecuta el updater, no modifica `update_last.json`, no relanza Nova y termina con código **5 — actualización ya en curso**.

La UI tampoco inicia un cierre local al pulsar **Actualizar**. Guarda la referencia al supervisor, marca `_update_supervisor_active`, deshabilita el botón y rechaza dobles pulsaciones mientras ese proceso siga vivo. El seguimiento usa `poll()` mediante `root.after()`; nunca bloquea Tk con `wait()`. Si el supervisor termina mientras el runtime original sigue abierto, la UI vuelve a consultar el resultado, lo muestra en la misma sesión y rehabilita el botón. Un `Popen()` fallido restaura inmediatamente el estado y Nova permanece abierta.

El supervisor sigue siendo la única autoridad que envía `shutdown_for_update`; ese comando llega a `RuntimeLifecycleManager`, que lo traduce a `request_shutdown("update")`. La coordinación es exception-safe para errores operacionales. Si falla antes de confirmar la terminación del runtime, no se ejecuta el updater ni se lanza otra instancia. Si el runtime ya terminó de forma verificable pero falla el guard, tampoco se actualiza: se libera cualquier guard retenido best-effort y se intenta exactamente un relanzamiento visible de recuperación con código `4`.

Después de una coordinación correcta el guard del runtime permanece adquirido durante staging, reemplazo, dependencias, validación y cualquier rollback. En rutas normales, incluso ante fallo del updater, el orden sigue siendo update/rollback → release guard → un solo `launch_nova(--post-update)`.

### Timeout y árbol de pip

`pip install -r requirements.txt` tiene timeout explícito de **15 minutos** por defecto; es inyectable para pruebas, rechaza valores no positivos/no finitos y limita valores excesivos a una hora. No usa `shell=True` ni existe un timeout externo que mate todo `nova_updater.py` en mitad de la transacción.

Pip se inicia en una unidad identificable: nueva sesión/grupo en Unix y nuevo process group en Windows. Nova usa `psutil` de forma recursiva en Windows y grupo de procesos en Unix para descubrir descendientes. Al vencer el timeout intenta terminación limpia, espera un periodo de gracia acotado, vuelve a inspeccionar el árbol, fuerza los procesos restantes, espera/reapea el proceso directo y verifica nuevamente. El mensaje **“detenidos y esperados de forma verificable”** solo se usa cuando esa comprobación termina sin procesos conocidos vivos y con inspección completa.

Si la terminación queda **confirmada**, pip se considera iniciado: Nova ejecuta rollback de archivos administrados, conserva el backup y persiste `dependencies_may_have_changed=true` y `recovery_required=true`. El rollback restaura modificados/eliminados, elimina únicamente archivos creados por la actualización y restaura `managed_files.json`, pero **no reconstruye exactamente `.venv`**.

Si la terminación de pip o sus descendientes **no puede confirmarse** después de la escalada normal y forzada, Nova entra en fail-closed: no inicia rollback concurrente, no declara `files_rollback_ok=true`, conserva backup/manifiesto, persiste `status=pip_termination_unconfirmed` y los PID restantes necesarios, y el supervisor **no relanza Nova desde esa `.venv`**. El updater usa código **6** para esta rama excepcional. El guard del runtime se mantiene mientras se agota la escalada y se libera después de que el updater ya decidió fail-closed; no se ejecuta Nova sobre un entorno que todavía pueda estar mutando.

`data/update_recovery.json` diferencia rollback completado, rollback incompleto e incertidumbre por terminación de pip. No guarda argumentos completos de procesos, tokens, contenido de archivos ni rutas externas sensibles.

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

Doctor informa lifecycle, ventana visible/oculta, bandeja lista/degradada, instancia única/scope, autostart real/conflicto y estado del updater residente. El chequeo del updater indica supervisor activo/inactivo mediante el mutex del kernel, último resultado disponible, recuperación pendiente y `pip_termination_unconfirmed`; los PID restantes solo aparecen cuando esa recuperación los necesita.

## Privacidad

Resident Mode no añade screenshots periódicos, captura de teclado, lectura de memoria de juegos, telemetría externa ni servidores de red. Los archivos de control no guardan prompts, títulos de ventana, contenido de pantalla, tokens ni secretos.

## Pruebas

Ubuntu ejecuta `compileall` y la suite completa. Windows ejecuta explícitamente lifecycle, owner/IPC, una integración con procesos separados reales para lock/comandos/terminación, el mutex del supervisor con dos procesos simultáneos y recuperación tras muerte del propietario, updater, rollback transaccional, session shutdown, Gaming Awareness, Instant Wake/hotkeys y core. Las pruebas de pip simulan timeout/terminate/kill/verificación sin sleeps reales.

La documentación técnica completa está en [`docs/v0.9.9-resident-runtime.md`](docs/v0.9.9-resident-runtime.md).

## Validación manual pendiente

Antes de publicar v0.9.9 deben comprobarse en Windows 11: X→bandeja, restauración por hotkey/bandeja, wake/F9 oculto, Gaming Mode + Qwen oculto, segunda ejecución, autostart real, conflicto de otra instalación, doble clic/arranques simultáneos de actualización, actualización real, fallo al iniciar el supervisor sin cerrar Nova, timeout recuperable de dependencias y una recuperación fail-closed controlada, salida completa y ausencia de servicios duplicados tras reiniciar.