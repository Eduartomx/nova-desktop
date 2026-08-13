# Nova Desktop

Nova es un asistente virtual local para Windows: Ollama + herramientas reales de escritorio/navegador + memoria/workspaces + Perception + Gaming Awareness + Skills + verificación y escalación experta opcional.

GitHub (`Eduartomx/nova-desktop`) es la fuente oficial. El actualizador estable sincroniza la Release publicada por tag, verifica Git blob SHA, crea backup y conserva rollback. Nova no depende de ZIPs de Release.

## Estado actual

**Nova v0.9.9 — Resident Mode & Runtime Lifecycle** está preparada en rama/PR para validación. La última base estable publicada es **v0.9.8 — Gaming Reliability** hasta que v0.9.9 sea aprobada y fusionada.

### v0.9.8 — Gaming Reliability

0.9.8 endureció Gaming Awareness:

- identifica el juego por PID/proceso y valida que la identidad siga viva;
- descarta Perception obsoleta y evita que launchers/helpers mantengan Gaming Mode;
- Wallpaper Engine es una exclusión obligatoria, incluso si una configuración antigua lo incluye como juego;
- la UI recibe cambios de Gaming Awareness mediante eventos Tk-safe;
- la restauración de Qwen está protegida contra reentradas y carreras de temporizadores;
- Ubuntu ejecuta la suite completa y Windows tiene una puerta específica de confiabilidad.

Gaming Awareness no lee memoria de juegos, no inyecta DLL/código y no necesita screenshots para detectar que estás jugando.

## v0.9.9 — Resident Mode & Runtime Lifecycle

El objetivo de 0.9.9 es que Nova pueda permanecer activa aunque la ventana principal esté oculta.

### Ciclo de vida

`RuntimeLifecycleManager` distingue explícitamente:

- `starting`
- `running`
- `hidden`
- `shutting_down`
- `stopped`

El botón **X** ya no significa necesariamente “terminar Nova”. Con Resident Mode habilitado y una bandeja funcional:

1. X ejecuta `withdraw()` sobre la ventana;
2. Nova desaparece de la barra de tareas;
3. los hotkeys siguen registrados;
4. wake word/F9 siguen disponibles;
5. Perception continúa;
6. Gaming Awareness continúa;
7. Qwen no se descarga solo por ocultar la ventana;
8. Tk permanece vivo y la ventana puede restaurarse.

Si la bandeja no puede iniciarse, Nova entra en modo degradado y **X vuelve a cerrar el proceso normalmente**. Esto evita dejar una instancia invisible sin forma de recuperarla.

Una salida real es distinta. **Salir de Nova** desde la bandeja ejecuta una secuencia idempotente y controlada: bloquea trabajo nuevo, detiene voz/wake word, Gaming/Perception, Browser Agent, guarda/flush de recursos que lo soportan, aplica `unload_on_exit` a Qwen, detiene la bandeja, libera la instancia y destruye Tk. Un fallo al cerrar un componente se registra de forma segura y no impide intentar cerrar los demás.

Windows logoff/shutdown se integra mediante `WM_QUERYENDSESSION`/`WM_ENDSESSION`: la consulta se responde rápido y la limpieza se solicita cuando Windows confirma el fin de sesión.

### Bandeja del sistema

Nova usa `pystray` (Pillow ya era dependencia del proyecto). El menú incluye:

- **Abrir Nova**;
- estado de Qwen;
- estado de Gaming Mode;
- **Precargar Qwen**;
- **Liberar Qwen**;
- **Buscar actualizaciones**;
- **Iniciar con Windows** (marcado según el estado real de Windows);
- **Salir de Nova**.

Las operaciones lentas de Qwen/update no se ejecutan en el hilo de Tk. Las acciones que muestran/ocultan la ventana vuelven al scheduler del lifecycle.

El icono es único por instancia. En esta primera versión se prioriza confiabilidad: el estado dinámico principal se refleja en el menú en lugar de depender de múltiples iconos visuales.

### Instancia única

Antes de cargar `core_runtime`, crear Agent, registrar hotkeys o precargar Qwen, `app.py` adquiere un bloqueo exclusivo por usuario/sesión bajo `%LOCALAPPDATA%\Nova\runtime` usando el bloqueo de archivos del kernel de Windows.

No es un PID file: el archivo persistente no decide quién es dueño de Nova; el **lock del kernel** es la fuente de verdad y se libera al terminar/crashear el proceso.

Una segunda ejecución:

1. no construye la UI/Agent;
2. escribe únicamente el comando local permitido `show`;
3. termina con código exitoso;
4. la instancia residente consume el comando y restaura la ventana.

El buzón local acepta solo `show`, `shutdown_for_update` y `status`, usa escritura atómica, rechaza payloads malformados y descarta comandos antiguos. No abre un servidor de red.

### Inicio con Windows

La opción está **desactivada por defecto** y nunca se activa durante una migración.

Cuando el usuario la habilita explícitamente, Nova usa únicamente:

`HKCU\Software\Microsoft\Windows\CurrentVersion\Run\NovaDesktop`

No requiere administrador ni instala un servicio. La entrada apunta a la instalación real del usuario y lanza:

```text
pythonw.exe <ruta real>\app.py --background
```

`--background` inicia Nova oculta solo si la bandeja está disponible. Los hotkeys, wake word, Perception y Gaming Awareness siguen activos.

Desactivar el inicio elimina únicamente el valor `NovaDesktop`. Nova Doctor y el menú consultan el estado real de HKCU; `config.json` no se usa como única fuente de verdad.

Comandos explícitos:

```text
Nova, ¿estás ejecutándote en segundo plano?
Nova, ocúltate en la bandeja
Nova, muéstrate
Nova, inicia con Windows
Nova, no inicies con Windows
```

Las órdenes de inicio con Windows usan coincidencias deterministas y explícitas; una conversación ambigua sobre “iniciar cosas con Windows” no cambia el sistema.

### Actualizaciones en modo residente

El botón **⬆ Actualizar** solicita `request_shutdown("update")`: no se limita a ocultar la ventana.

`update_runner.py` puede también solicitar `shutdown_for_update` a una instancia residente y espera a que el lock real quede libre antes de modificar archivos. Después ejecuta el updater GitHub-native, conserva sus logs/backup/rollback y relanza **una sola** instancia con `--post-update` para que el resultado sea visible.

`ACTUALIZAR_NOVA.cmd` sigue usando el mismo supervisor. Una ejecución interactiva directa de `nova_updater.py` también se delega al supervisor; `--yes` queda como ruta interna/no interactiva usada después de que el supervisor ya coordinó el cierre.

Si la actualización falla, Nova se relanza visible y `data/update_last.json` permite mostrar el error y el log; no debe quedar únicamente escondida en la bandeja.

### Notificaciones y privacidad

Las notificaciones de bandeja están limitadas a eventos útiles, por ejemplo:

- tarea terminada mientras Nova estaba oculta;
- actualización disponible;
- actualización terminada;
- error importante que requiere abrir Nova.

No se notifican inferencias normales, ticks de Gaming Awareness, cambios de ventana, cambios de proceso ni precargas habituales de Qwen. Existe cooldown/deduplicación por tipo de evento.

Las notificaciones no incluyen prompts, respuestas, títulos completos de ventana, texto de pantalla, tokens, API keys ni secretos.

## Atajos y voz

Los defaults actuales se conservan:

- abrir Nova: **Ctrl + Alt + N**;
- contexto: **Ctrl + Alt + Shift + N**;
- push-to-talk: **F9**;
- STT local con faster-whisper;
- TTS local;
- wake word local mediante openWakeWord cuando el modelo configurado está disponible.

Tus hotkeys personalizados de `config.json` no son reemplazados por Resident Mode.

## Configuración Resident Mode

Ejemplo:

```json
"resident_mode": {
  "enabled": true,
  "close_to_tray": true,
  "start_with_windows": false,
  "start_hidden": false,
  "notifications": true
}
```

`start_with_windows=false` es el default. Cambiar este valor mediante una migración no crea automáticamente la entrada HKCU; el cambio real del sistema requiere una acción explícita del usuario.

## Nova Doctor

Doctor informa, además de los componentes anteriores:

- Resident Mode habilitado/deshabilitado;
- bandeja activa o degradada;
- estado de la instancia única;
- inicio con Windows real;
- ventana visible/oculta;
- último motivo de cierre disponible;
- errores recientes del lifecycle sin guardar contenido sensible.

## Si el icono de bandeja no aparece

1. Abre el área de iconos ocultos de Windows.
2. Comprueba Nova Doctor → **Resident Mode**.
3. Si Doctor indica bandeja degradada, **no cierres con X esperando que quede residente**: Nova está diseñada para terminar normalmente cuando no puede garantizar recuperación desde la bandeja.
4. Comprueba que `pystray` esté instalado en el mismo `.venv` de Nova. El updater instala dependencias nuevas desde `requirements.txt` cuando cambia una Release.
5. Reinicia Nova y vuelve a diagnosticar antes de activar `start_hidden`.

## Prueba manual de v0.9.9 en Windows

1. Abrir Nova normalmente.
2. Pulsar X y confirmar que desaparece de la barra de tareas, pero permanece en la bandeja.
3. Abrirla con el hotkey configurado.
4. Ocultarla y restaurarla desde **Abrir Nova** en la bandeja/doble clic.
5. Comprobar wake word y F9 con la ventana oculta.
6. Abrir un juego y verificar que Gaming Mode sigue funcionando con Nova oculta.
7. Ejecutar Nova una segunda vez y confirmar que solo se muestra la instancia existente.
8. Activar **Iniciar con Windows**, cerrar sesión/reiniciar sesión y comprobar arranque oculto.
9. Desactivar inicio con Windows y comprobar en Doctor/bandeja que ya no está presente.
10. Usar **Buscar actualizaciones** desde la bandeja sin instalar una versión inexistente.
11. Elegir **Salir de Nova** y confirmar que no queda ningún proceso Nova.
12. Volver a iniciar y verificar que no existen hotkeys/wake/Gaming duplicados.

## Desarrollo y publicación

Los PR ejecutan `compileall`, suite completa en Ubuntu y una validación dirigida en `windows-latest`. Resident Mode añade repeticiones de las pruebas sensibles a carreras para lifecycle/updater, además de conservar Gaming Awareness, Instant Wake, hotkeys y bootstrap.

Fusionar un nuevo `VERSION` a `main` dispara `Publish Nova Native Release`, que vuelve a compilar/probar antes de crear la Release estable. La rama de v0.9.9 no debe publicar nada hasta ser aprobada y fusionada.

## Privacidad general

No deben subirse `config.json` real, `data/`, bases SQLite, perfil del navegador, screenshots, logs personales, `.venv/`, tokens ni API keys. Perception no captura teclado/portapapeles/screenshots periódicamente. Event-driven Vision solo captura bajo petición/evento permitido. Instant Wake usa una petición local vacía. Gaming Awareness usa metadatos locales de proceso/ventana/GPU y Resident Mode añade únicamente estado de lifecycle, bandeja, lock local y preferencias de inicio; no añade captura de pantalla ni telemetría externa.
