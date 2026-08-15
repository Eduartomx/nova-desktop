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

## Updater residente seguro

`update_runner.py` adquiere primero `update_supervisor.lock`, antes de leer el runtime o enviar `shutdown_for_update`. Para una transacción que llega a un estado validado, el orden obligatorio es:

```text
supervisor mutex
→ coordinación + runtime/recovery guard
→ transacción/recovery deja journal validado activo
→ verificar attempt_id + generation + estado
→ iniciar helper desde bootstrap estable hash-validado
→ liberar runtime/recovery guard
→ CAS del intento exacto a cleared
→ helper observa ese cleared y relanza Nova una sola vez
→ liberar supervisor mutex
```

El orden inverso no se usa. Si UI, `ACTUALIZAR_NOVA.cmd` o ejecución directa intentan iniciar simultáneamente otro supervisor, solo uno puede poseer el mutex. El segundo no envía `shutdown_for_update`, no ejecuta el updater, no modifica `update_last.json`, no relanza Nova y termina con código **5 — actualización ya en curso**.

La UI no inicia un cierre local al pulsar **Actualizar**. Guarda la referencia al supervisor, marca `_update_supervisor_active`, deshabilita el botón y rechaza dobles pulsaciones mientras ese proceso siga vivo. El seguimiento usa `poll()` mediante `root.after()` y nunca bloquea Tk con `wait()`.

El supervisor sigue siendo la única autoridad que envía `shutdown_for_update`; ese comando llega a `RuntimeLifecycleManager`, que lo traduce a `request_shutdown("update")`. Después de una coordinación correcta, el guard del runtime permanece adquirido durante staging, journal, reemplazo, dependencias, validación y rollback; solo el handoff validado lo consume antes del CAS terminal.

### Motor interno sin entrypoint privilegiado

El motor de actualización ya no se autoriza con `--yes`. `update_runner.py`, mientras posee el mutex del supervisor y el runtime guard, importa `resident_update_engine` y ejecuta la transacción **en el mismo proceso**.

`python updater/resident_update_engine.py --yes` devuelve un error seguro antes de consultar GitHub, crear staging, tocar archivos o iniciar pip. El updater histórico `nova_updater_legacy.py` permanece únicamente como implementación importable de compatibilidad y su ejecución directa queda interceptada antes de sus side effects. `nova_updater.py`, la UI y los scripts `.cmd` siguen delegando al supervisor.

### Journal antes de toda mutación

La transacción publica un journal durable inmediatamente después de crear y validar el backup y **antes** de reemplazar/eliminar un archivo, modificar `managed_files.json`, ejecutar pip o validar la nueva instalación.

Orden esencial:

```text
crear + validar backup
→ publicar schema 2: transaction_prepared
→ preparar bootstrap estable
→ si requirements cambia: capturar snapshot anterior de dependencias
→ files_applying  (files_may_have_changed=true)
→ aplicar archivos + managed_files
→ files_applied
→ si corresponde: dependencies_starting
   (dependencies_may_have_changed=true ANTES de ejecutar pip)
→ dependencies_running
→ update_validation_in_progress
→ update_validated  (el motor termina aquí)
→ handoff del supervisor
→ cleared
```

Una muerte anterior a la creación del backup/journal no implica mutación de la instalación. Una muerte posterior a `transaction_prepared` deja una barrera persistente y el supervisor no decide relanzar Nova únicamente por el código de salida del motor: vuelve a inspeccionar el journal durable.

Existe además una recuperación específica para el único hueco previo a mutación: si el proceso muere después de `transaction_prepared` pero antes de publicar `data/recovery_runtime/`, y `files_may_have_changed=false`, el bootstrap puede reconstruir de forma segura la generación estable desde la fuente todavía intacta. Una vez que los archivos pueden haber cambiado, esa reconstrucción desde el árbol administrado queda prohibida.

### Máquina de estados schema 2 + CAS

`data/update_recovery.json` usa `schema_version=2`, `attempt_id` obligatorio y `generation` monotónica. Cada transición ocurre bajo un lock de journal del sistema operativo, vuelve a leer el estado actual y aplica compare-and-swap sobre `attempt_id + generation`. Un escritor obsoleto no puede sobrescribir un intento distinto, un rollback más avanzado, `dependency_repair_required` ni un estado ya limpiado por otra generación.

Estados actuales:

```text
transaction_prepared
files_applying
files_applied
dependencies_starting
dependencies_running
update_validation_in_progress
update_validated
pip_termination_unconfirmed
waiting_for_processes
rollback_in_progress
rollback_completed
rollback_validation_in_progress
rollback_validation_completed
dependency_repair_required
cleared
```

`cleared` es **terminal**: no admite transición a recovery ni siquiera una escritura `cleared → cleared`. Un fallo posterior de `Popen` se registra fuera del journal y no puede reabrir la transacción terminal.

El grafo de transiciones es explícito; no se aceptan saltos arbitrarios. Las escrituras usan temporal + `flush` + `fsync` cuando corresponde + `os.replace`. Journals schema 1 conocidos se migran de forma explícita; JSON truncado, esquema desconocido o estados no migrables fallan cerrados.

El journal no contiene comandos completos, variables de entorno, tokens ni contenido de archivos. Los errores se sanitizan. `backup_path` y cualquier snapshot se guardan como referencias relativas autorizadas, no como rutas externas confiadas ciegamente.

### Contención autoritativa de pip en Windows

`pip install -r requirements.txt` conserva un timeout explícito de **15 minutos** por defecto; es inyectable para pruebas, rechaza valores no positivos/no finitos y limita valores excesivos a una hora. No usa `shell=True`.

En Windows Nova no usa `psutil.children(recursive=True)` como prueba de que todo el árbol terminó. Pip se crea **suspendido** con `CreateProcessW`; antes de ejecutar una sola instrucción se crea/configura un **Windows Job Object** con `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, se asigna el proceso con `AssignProcessToJobObject` y solo entonces se reanuda el thread principal con `ResumeThread`.

Si la creación/configuración/asignación del Job Object falla antes de reanudar pip, pip no se considera iniciado y no existe downgrade silencioso a `psutil` como garantía. Las pruebas Windows incluyen además la muerte abrupta del updater mientras el Job contiene un proceso hijo: el hijo no puede sobrevivir al cierre del Job.

En Unix se conserva nueva sesión/grupo de procesos, señales y verificación mediante identidad fuerte.

### Identidades fuertes

Los procesos pendientes no se representan únicamente por PID. El journal usa identidades equivalentes a:

```json
{
  "pid": 1234,
  "creation_time": 1720000000123456,
  "role": "pip_root_or_descendant"
}
```

Para considerar que un proceso registrado sigue vivo deben coincidir PID **y** tiempo de creación normalizado. Un PID reutilizado con otro `creation_time` no bloquea la recuperación y nunca se termina basándose únicamente en ese PID. Los errores de acceso/inspección se tratan de forma conservadora.

### Cuarentena persistente y códigos 6/7

Si pip llegó a arrancar y no puede demostrarse la terminación del contenedor/árbol, la transacción devuelve **6 — `pip_termination_unconfirmed`**. No inicia rollback concurrente, no relanza Nova y conserva backup, journal e identidades fuertes.

La cuarentena sobrevive al cierre del supervisor, reinicios de Nova y reinicios de Windows. Mientras exista un intento activo —o el journal no pueda validarse— el startup y una actualización nueva quedan bloqueados con **7 — `recovery_required_or_in_progress`**.

El updater gate no sobrescribe el journal mientras los procesos registrados siguen vivos. Una salida inesperada del motor tampoco habilita el relanzamiento: si al volver del motor existe un journal activo/corrupto, el supervisor devuelve 7 y conserva backup/journal.

### Snapshot y validación fuerte de dependencias

Si `requirements.txt` cambiará, Nova captura antes de aplicar archivos un snapshot del entorno Python anterior dentro del backup autorizado. El snapshot contiene nombres de distribuciones normalizados, **versiones exactas instaladas**, conjunto anterior, hash SHA-256 del propio snapshot, hash del `requirements.txt` anterior y la lista acotada de imports críticos aplicables.

Tras restaurar archivos:

- si pip nunca pudo ejecutar código, no se exige una reparación de dependencias innecesaria;
- si pip pudo ejecutarse, el conjunto actual debe coincidir con el snapshot: una distribución añadida, eliminada o con versión distinta bloquea la limpieza;
- el `requirements.txt` restaurado debe conservar su hash anterior;
- imports críticos se prueban en un subprocess aislado y acotado con `python -I`;
- recovery **no ejecuta pip** para corregir diferencias.

Si la comparación/imports no permiten demostrar compatibilidad suficiente, el journal avanza a `dependency_repair_required`; conserva cuarentena y backup y no relanza Nova.

Esto es deliberadamente más fuerte que comprobar `importlib.metadata.version(name)` para saber si un paquete “existe”, pero **no equivale a un rollback transaccional de `.venv`**. El proyecto no inventa versiones/hashes para un lockfile porque el `requirements.txt` actual no está completamente fijado.

### Bootstrap estable de recuperación

Antes de tocar archivos, el updater prepara una generación stdlib-only en:

```text
data/recovery_runtime/
  active.json
  generations/<generation>/
    manifest.json
    process_launch.py
    recovery_bootstrap.py
    recovery_handoff.py
    recovery_state.py
    recovery_journal.py
    recovery_attempts.py
    recovery_files.py
    recovery_environment.py
    recovery_locking.py
```

Cada generación se escribe completa, se verifica por SHA-256 y solo después se reemplaza atómicamente `active.json`. La generación conocida como buena anterior no se sobreescribe en sitio. `process_launch.py` y `recovery_handoff.py` forman parte del conjunto exacto hash-validado; una copia alterada impide crear el helper.

La selección de intérprete se centraliza en `process_launch.py`: los procesos internos usan el Python de consola del entorno sin abrir ventana, mientras el relanzamiento final prefiere `pythonw.exe` del mismo entorno. La UI inicia el supervisor con `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP`, conserva `poll()` no bloqueante y escribe la salida temprana en `data/updater_logs`.

En `app.py` el orden es **recovery gate antes de `_claim_instance`**. Se intenta primero el bootstrap administrado; si su import/ejecución falla y existe journal, se carga únicamente la generación estable cuyo manifest y archivos coinciden con sus hashes. Si ambas copias fallan, `app.py` usa `MessageBoxW` directamente en Windows y sale con 7, sin depender de stderr/pythonw, sin reclamar instancia y sin importar Tk completo, Agent ni módulos normales del asistente.

### Handoff validado y recuperación reanudable

Tanto un update correcto como un recovery correcto usan el mismo mecanismo compartido en `recovery_handoff.py`. El motor nunca limpia el journal: termina en `update_validated` o `rollback_validation_completed`.

El handoff realiza:

```text
supervisor mutex retenido
→ runtime/recovery guard retenido
→ releer journal y verificar attempt_id + generation + estado validado
→ validar por SHA el bootstrap estable completo
→ Popen del helper estable mientras el journal sigue activo
→ crash hook de prueba, si existe
→ liberar runtime/recovery guard
→ CAS exacto validated → cleared con token de handoff
→ helper observa cleared del mismo attempt/generation/token
→ helper ejecuta app.py --post-update o --post-recovery exactamente una vez
→ liberar supervisor mutex
```

El helper no acepta otro `attempt_id`, otra generación, otro estado, otro token ni un journal corrupto. Mientras el estado validado sigue activo, espera de forma acotada y **no lanza Nova**.

Si el helper no puede crearse, no se libera/limpia el intento. Si falla la liberación del guard, no existe CAS a `cleared`. Si un escritor vuelve obsoleta la generación antes del CAS, el clear falla y el helper queda sin autorización. Si el supervisor muere después de crear el helper pero antes del CAS, la cuarentena queda activa y el helper expira sin lanzar. Si muere después del CAS, el helper independiente ya autorizado puede relanzar Nova.

Después de `cleared`, el journal es terminal. Si el `Popen` final de Nova falla, el helper registra el fallo en `data/updater_logs/recovery_handoff.log`; no reescribe ni reabre `update_recovery.json`.

El restaurador **no administra ni borra journals**. Restaura modificados/eliminados, elimina solo `created_new`, restaura `managed_files.json` y vuelve a validar traversal/symlinks en cada reanudación. Una muerte a mitad del rollback permite repetirlo de forma segura.

### Códigos principales

- `0`: operación correspondiente completada.
- `3`: camino sin transacción durable completado pero falló su relanzamiento heredado.
- `4`: coordinación no verificable o entrypoint interno directo bloqueado.
- `5`: otro supervisor ya está activo.
- `6`: pip pudo ejecutar y su terminación no pudo confirmarse.
- `7`: recuperación obligatoria/activa/corrupta, handoff no autorizado o motor terminó con intento durable todavía activo.

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

Doctor informa lifecycle, ventana visible/oculta, bandeja lista/degradada, instancia única/scope, autostart real/conflicto y estado del updater residente. El estado activo/inactivo del supervisor proviene del mutex del kernel. Si hay cuarentena, informa su estado; las identidades/PID restantes solo aparecen cuando son necesarias para diagnosticar espera de procesos.

## Privacidad

Resident Mode y recovery no añaden screenshots, captura de teclado, memoria de juegos, telemetría externa ni servidores de red. Journal/snapshot no guardan comandos completos, variables de entorno, tokens, prompts ni contenido de archivos.

## Pruebas

Ubuntu y Windows ejecutan primero el gate focal del handoff (`test_recovery_handoff`, terminal state, update runner, bootstrap, stable bootstrap, crash recovery y multiprocess), luego `compileall`/suite completa y las regresiones explícitas. Windows ejecuta además Job Object real, carrera root→child repetida, muerte abrupta del updater con proceso dentro del Job sin permitir `skip`, crash reales mediante `os._exit`, dos recovery supervisors en procesos reales, muerte del dueño del lock y reanudación, lifecycle, IPC, session shutdown, Gaming Awareness, Instant Wake/hotkeys y core.

Las pruebas de recovery/handoff incluyen:

- `tests/test_recovery_handoff.py`
- `tests/test_recovery_terminal_state.py`
- `tests/test_recovery_pre_mutation_bootstrap.py`
- `tests/test_recovery_bootstrap.py`
- `tests/test_recovery_update_gate.py`
- `tests/test_pip_job_object.py`
- `tests/test_dependency_snapshot.py`
- `tests/test_stable_recovery_bootstrap.py`
- `tests/test_updater_entrypoint_guards.py`
- `tests/test_update_crash_recovery.py`
- `tests/test_recovery_multiprocess.py`

La documentación técnica completa está en [`docs/v0.9.9-resident-runtime.md`](docs/v0.9.9-resident-runtime.md).

## Recuperación manual y diagnóstico

Ante cuarentena, **no borres `data/update_recovery.json`, `data/updater_backups/` ni `data/recovery_runtime/`**. Consulta el estado con:

```text
python updater/recovery_bootstrap.py --status
```

Si el bootstrap administrado no funciona, el aviso nativo de `app.py` muestra el comando apuntando a la generación estable validada. La recuperación manual soportada usa el mismo bootstrap con `--recover`; borrar el journal a mano elimina la barrera fail-closed y no es un procedimiento soportado.

`dependency_repair_required` significa que los archivos pudieron restaurarse pero el entorno Python ya no coincide suficientemente con el snapshot anterior. Conserva el backup/journal y reconstruye o repara la `.venv` de forma explícita antes de retirar la cuarentena; recovery no intenta adivinar qué debe desinstalar o degradar con pip.

## Riesgos residuales

- El rollback cubre archivos administrados y valida el entorno; no crea un snapshot bit-a-bit de `.venv`.
- Daño de disco/I/O, corrupción fuera del conjunto administrado o cambios externos concurrentes pueden requerir reparación manual.
- El snapshot exacto detecta diferencias de distribuciones/versiones, pero no convierte un `requirements.txt` no fijado en un entorno reproducible.
- Después de un `cleared` terminal, un fallo del proceso final de Nova queda registrado pero no puede reabrir automáticamente la transacción; requiere diagnóstico/reintento normal del usuario.
- Ante cualquier identidad, journal, backup, manifest, hash o validación que no pueda demostrarse, la política es mantener la cuarentena en lugar de asumir éxito.

## Validación manual pendiente

Antes de aprobar v0.9.9 deben probarse en una **instalación/fixture descartable de Windows 11**, no dañando la instalación principal: actualización normal; doble clic; timeout de pip confirmado; terminación no confirmada simulada; persistencia de cuarentena tras reiniciar Nova/Windows; bloqueo de startup/update; desaparición de identidades fuertes; recovery reanudado; snapshot de dependencias; bootstrap estable; handoff post-update/post-recovery; muerte del supervisor antes/después del CAS; restauración/validación; un solo relanzamiento; y fallo simulado de recovery conservando journal/backup.
