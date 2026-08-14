# Nova Desktop

Nova es un asistente virtual local para Windows con Ollama, herramientas de escritorio/navegador, memoria y workspaces, Perception, Gaming Awareness, Skills, voz local y updater GitHub-native.

GitHub (`Eduartomx/nova-desktop`) es la fuente de verdad. El updater estable sincroniza código desde la Release/tag publicada, valida Git blob SHA y conserva backup/rollback; no depende de ZIPs.

## Estado

La base publicada es **v0.9.8 — Gaming Reliability**. La rama de desarrollo prepara **v0.9.9 — Resident Mode & Runtime Lifecycle**; no debe publicarse hasta validación manual y aprobación explícita.

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

`update_runner.py` adquiere primero `update_supervisor.lock`, antes de leer el runtime o enviar `shutdown_for_update`. El orden normal obligatorio es:

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

Después de una coordinación correcta el guard del runtime permanece adquirido durante staging, reemplazo, dependencias, validación y cualquier rollback.

### Contención autoritativa de pip en Windows

`pip install -r requirements.txt` conserva un timeout explícito de **15 minutos** por defecto; es inyectable para pruebas, rechaza valores no positivos/no finitos y limita valores excesivos a una hora. No usa `shell=True`.

En Windows Nova ya no usa `psutil.children(recursive=True)` como prueba de que todo el árbol terminó. El proceso de pip se crea **suspendido** con `CreateProcessW`; antes de ejecutar una sola instrucción se crea y configura un **Windows Job Object** con `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, se asigna el proceso con `AssignProcessToJobObject` y solo entonces se reanuda su thread principal con `ResumeThread`.

Las APIs y estructuras Win32 usadas por esta ruta tienen firmas `ctypes` explícitas. Los handles de proceso, thread y Job Object se cierran en todos los caminos. Si la creación/configuración/asignación del Job Object falla antes de reanudar pip, pip no se considera iniciado, no existe downgrade silencioso a `psutil` como garantía y el rollback de archivos puede continuar con seguridad. En entornos con jobs anidados se intenta únicamente una ruta nativa compatible todavía suspendida; si no puede obtenerse contención autoritativa, la instalación de dependencias falla antes de ejecutar pip.

El cierre del Job Object y sus consultas nativas son la autoridad para afirmar la terminación en Windows. `psutil` puede seguir ayudando en observabilidad, pero no convierte por sí solo una terminación en confirmada.

En Unix se conserva la estrategia de nueva sesión/grupo de procesos, señales y verificación de identidad fuerte.

### Identidades fuertes

Los procesos pendientes no se representan únicamente por PID. El journal usa identidades equivalentes a:

```json
{
  "pid": 1234,
  "creation_time": 1720000000123456,
  "role": "pip_root_or_descendant"
}
```

Para considerar que un proceso registrado sigue vivo deben coincidir PID **y** tiempo de creación normalizado. Un PID reutilizado con otro `creation_time` no bloquea la recuperación y nunca se termina basándose únicamente en ese PID. Los errores de acceso/inspección se tratan de forma conservadora y mantienen la cuarentena.

### Cuarentena persistente

Si pip llegó a arrancar y no puede demostrarse la terminación del contenedor/árbol, la transacción devuelve **código 6 — `pip_termination_unconfirmed`**. En ese momento Nova:

- no inicia rollback concurrente;
- no declara que los archivos fueron restaurados;
- no relanza Nova;
- conserva backup y manifiesto;
- crea o actualiza atómicamente `data/update_recovery.json`;
- marca recuperación obligatoria y dependencia potencialmente modificada.

La cuarentena es persistente: sobrevivirá al cierre del supervisor, a un nuevo intento de abrir Nova y a un reinicio de Windows. Mientras exista un journal válido pendiente —o el journal sea corrupto/desconocido— el arranque normal y una nueva actualización quedan bloqueados con **código 7 — `recovery_required_or_in_progress`**.

Un nuevo updater no sobrescribe el journal ni destruye el backup anterior. Primero intenta el recovery gate bajo el mutex del supervisor. Si las identidades fuertes aún siguen vivas o no pueden comprobarse, devuelve 7 sin descargar archivos ni iniciar pip.

### Journal de recuperación

`data/update_recovery.json` usa esquema versionado y escritura atómica mediante archivo temporal, `flush`, `fsync` cuando corresponde y `os.replace`. Incluye `attempt_id`, `generation`, estado, timestamps, `recovery_required`, referencia al backup, incertidumbre de dependencias, estado del rollback, identidades fuertes pendientes y errores técnicos sanitizados.

Estados de recuperación soportados:

```text
pip_termination_unconfirmed
waiting_for_processes
rollback_in_progress
rollback_completed
validation_in_progress
validation_completed
cleared
```

JSON truncado/corrupto, esquema desconocido o campos de identidad inválidos producen fail-closed. El `backup_path` no se usa ciegamente: debe resolver dentro de la raíz autorizada de backups, sin traversal ni escape mediante symlinks, y el manifiesto interno vuelve a validar rutas relativas antes de restaurar.

### Gate extremadamente temprano del arranque

`nova/app.py` ejecuta el recovery gate antes de `_claim_instance`, antes de importar Tk, antes de `assistant.core_runtime`, antes de Agent/UI y antes de iniciar servicios normales.

El flujo es:

```text
stdlib bootstrap
→ leer/validar journal
→ comprobar identidades fuertes
→ si hay recuperación segura: supervisor mutex
→ runtime/recovery guard
→ rollback reanudable
→ validación
→ clear del journal
→ release runtime/recovery guard
→ un solo launch --post-recovery
→ release supervisor mutex
→ solo entonces puede existir un arranque normal
```

Si hay procesos registrados todavía vivos, errores de inspección o journal inválido, Nova muestra un aviso mínimo y sale con 7 sin cargar el asistente completo. Si el propio bootstrap mínimo no puede importarse y existe un journal, `app.py` también falla cerrado.

### Recuperación reanudable e idempotente

Cuando las identidades pendientes dejan de estar vivas, solo un proceso puede recuperar. Bajo `update_supervisor.lock` y un guard exclusivo de runtime/recovery se vuelve a leer el journal, se valida el backup y se marca `rollback_in_progress` de forma atómica.

El rollback restaurador es idempotente: restaura archivos modificados y eliminados desde backup, elimina únicamente archivos `created_new`, restaura el estado previo de `managed_files.json` y valida cada destino contra las raíces autorizadas. **No ejecuta pip durante recovery.** Si el proceso muere a mitad del rollback, el estado y backup permanecen y una ejecución posterior puede repetir la restauración de forma segura.

Después se marca `rollback_completed`, se valida la instalación restaurada sin instalar dependencias, se marca `validation_completed` y solo tras éxito se limpia la cuarentena. El launch posterior usa `--post-recovery` y se intenta una sola vez. Si ese launch falla, la cuarentena se restablece en `validation_completed` para que el siguiente intento no repita innecesariamente el rollback y vuelva a intentar únicamente el arranque.

Si rollback o validación fallan, el journal y backup se conservan, se registra un error sanitizado y Nova no arranca normalmente.

### Alcance real del rollback

El rollback transaccional cubre **archivos administrados por Nova**. Restaura modificados/eliminados, elimina solo archivos creados por la actualización fallida y restaura `managed_files.json`.

Esto **no reconstruye exactamente `.venv`**. Si pip llegó a iniciarse, el entorno de dependencias puede haber cambiado aunque los archivos hayan vuelto a su estado anterior. La cuarentena/recovery evita ejecutar sobre un entorno que todavía esté mutando; no constituye un snapshot transaccional completo de todos los paquetes Python.

### Códigos principales del updater residente

- `0`: actualización/recovery correspondiente completado correctamente.
- `3`: actualización correcta pero falló el relanzamiento normal.
- `4`: coordinación del runtime no verificable.
- `5`: otro supervisor de actualización ya está activo.
- `6`: la transacción actual inició pip y no pudo confirmar su terminación; se creó cuarentena persistente.
- `7`: existe recuperación obligatoria, está en curso o no puede validarse con seguridad; no se permite arranque/update normal.

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

Doctor informa lifecycle, ventana visible/oculta, bandeja lista/degradada, instancia única/scope, autostart real/conflicto y estado del updater residente. El chequeo del updater indica supervisor activo/inactivo mediante el mutex del kernel, último resultado disponible y recuperación pendiente. Si hay cuarentena, informa su estado; las identidades/PID restantes solo aparecen cuando son necesarias para diagnosticar `pip_termination_unconfirmed`/`waiting_for_processes`.

## Privacidad

Resident Mode y el recovery journal no añaden screenshots, captura de teclado, memoria de juegos, telemetría externa ni servidores de red. El journal no guarda comandos completos, variables de entorno, tokens, prompts ni contenido de archivos. Los errores se sanitizan y las identidades persistidas se limitan a PID, tiempo de creación y rol técnico.

## Pruebas

Ubuntu ejecuta `compileall`, la suite completa y las suites explícitas del updater/recovery. Windows ejecuta además Job Object real, escenario root→child, repetición de la carrera, lifecycle, owner/IPC, mutex multiproceso, rollback, recovery bootstrap, gates, session shutdown, Gaming Awareness, Instant Wake/hotkeys y core. Las pruebas de recuperación usan eventos/locks/fixtures en lugar de pausas arbitrarias como mecanismo principal de sincronización.

La documentación técnica completa está en [`docs/v0.9.9-resident-runtime.md`](docs/v0.9.9-resident-runtime.md).

## Recuperación manual y diagnóstico

La primera acción ante una cuarentena es **no borrar `data/update_recovery.json` ni el backup**. Puede consultarse el estado con el recovery bootstrap de la misma instalación y Nova Doctor cuando el arranque normal vuelva a ser seguro. Si el journal indica procesos pendientes, debe esperarse a que desaparezca la identidad fuerte original; no se debe matar un proceso únicamente porque reutilizó el mismo PID.

Si la recuperación automática vuelve a fallar, conserva `data/update_recovery.json`, `data/updater_backups/` y los logs del updater para diagnóstico. La reparación manual debe hacerse sobre una copia/instalación de prueba o restaurando explícitamente desde el backup validado; borrar la cuarentena a mano sin resolver el estado de dependencias elimina la barrera fail-closed y no es un procedimiento soportado.

## Validación manual pendiente

Antes de aprobar v0.9.9 deben probarse en una **instalación/fixture descartable de Windows**, no dañando la instalación principal: actualización normal; doble clic; timeout de pip con terminación confirmada; terminación no confirmada simulada; creación y persistencia de cuarentena; reinicio de Nova y de Windows con cuarentena; bloqueo de startup/update; desaparición de identidades fuertes; recovery reanudado; restauración/validación; un solo relanzamiento `--post-recovery`; y fallo de recovery conservando journal/backup.
