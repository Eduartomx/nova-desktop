# Nova Desktop

Nova es un asistente virtual local para Windows: Ollama + herramientas reales de escritorio/navegador + memoria/workspaces + percepción + Skills + verificación y escalación experta opcional.

## Estado actual

**Nova v0.9.5 — Gaming Awareness**

GitHub es la fuente oficial. Las Releases estables se sincronizan por tag mediante el updater nativo, con verificación de Git blob SHA, backup y rollback. No hacen falta ZIPs de Release.

## Qué cambia en 0.9.5

0.9.5 hace que Instant Wake sea consciente de juegos y de la presión de VRAM:

- `GamingAwarenessManager` combina Perception Engine con procesos conocidos, rutas de bibliotecas Steam/Xbox/Epic y señales de Minecraft Java;
- usa tiempos de permanencia al entrar/salir para evitar rebotes por alt-tab o procesos breves;
- la política `smart` libera Qwen cuando el juego está en primer plano o existe presión real de VRAM;
- antes de liberar el modelo suspende nuevas precargas y aplica temporalmente `keep_alive=0` a las inferencias locales;
- una inferencia activa nunca se interrumpe para descargar Qwen;
- si Nova necesita responder mientras juegas, Qwen puede cargarse para esa consulta y volver a liberarse después;
- al salir del juego, Nova restaura la política normal y puede precargar Qwen otra vez si Gaming Mode fue quien lo descargó;
- Perception reduce temporalmente su polling de 1100 ms a 2500 ms durante Gaming Mode;
- el nuevo botón **🎮 Juego** permite modo automático/forzado/desactivado, política `smart/always/never` y decidir si Qwen debe permanecer cargado;
- Nova Doctor muestra juego detectado, razón de la política de VRAM, VRAM observada/recuperada y frecuencia efectiva de Perception.

Comandos útiles:

```text
Nova, ¿estás en modo juego?
Nova, activa modo juego.
Nova, desactiva modo juego.
Nova, vuelve a modo juego automático.
Nova, mantén Qwen cargado aunque esté jugando.
Nova, libera Qwen cuando juegue.
Nova, ¿por qué liberaste Qwen?
```

Gaming Awareness no lee memoria del juego, no inyecta DLLs/código, no captura teclado y no usa screenshots para detectar juegos.

## Qué cambia en 0.9.4

0.9.4 usa las métricas obtenidas en 0.9.3 para atacar el cold start sin cambiar de modelo:

- `LLMWarmManager` precarga Qwen localmente en segundo plano al iniciar Nova mediante una petición vacía de Ollama;
- las inferencias normales renuevan un `keep_alive` centralizado de 20 minutos por defecto, evitando recargas innecesarias sin reservar VRAM indefinidamente;
- Nova consulta `/api/ps` para mostrar si el modelo está cargado, la VRAM reportada por Ollama y su expiración;
- `Nova, ¿Qwen está cargado?`, `Nova, precarga Qwen` y `Nova, libera la VRAM` son rutas locales deterministas;
- al cerrar Nova el modelo se descarga de Ollama por defecto;
- el atajo principal pasa a **Ctrl+Alt+N** y el de contexto a **Ctrl+Alt+Shift+N**;
- los antiguos defaults basados en Espacio se migran automáticamente;
- el botón **⚙ Atajos** permite validar, guardar y aplicar nuevas combinaciones globales sin reiniciar Nova.

La precarga usa `messages: []`: no envía prompts, respuestas ni resultados a servicios externos.

## Qué cambia en 0.9.3

0.9.3 convierte la latencia de Ollama en un problema medible en vez de una única cifra agregada:

- cada llamada local a `/api/chat` captura las métricas que Ollama ya devuelve: `total_duration`, `load_duration`, `prompt_eval_count`, `prompt_eval_duration`, `eval_count` y `eval_duration`;
- Nova calcula velocidad de evaluación/generación, tokens de entrada/salida, tamaño estructural del contexto, cantidad de mensajes y cantidad de tools expuestas;
- en NVIDIA puede tomar una muestra puntual de GPU/VRAM antes y después de la inferencia mediante `nvidia-smi`; no existe polling agresivo de GPU por esta función;
- `data/llm_performance.db` persiste únicamente métricas técnicas. No guarda prompts, respuestas, argumentos de herramientas ni secretos;
- Performance Profiler añade `session_id`, por lo que Nova Doctor separa **sesión actual**, **15 min**, **1 h** y **24 h** en vez de mezclar una versión recién actualizada con eventos históricos;
- Nova clasifica causas probables como cold start, prompt/contexto pesado, generación lenta, presión de VRAM, demasiadas tools o una proporción elevada de timeouts;
- `Nova, prueba tu rendimiento` ejecuta un benchmark local explícito y acotado con tres casos: respuesta corta, razonamiento breve y selección con tools;
- el benchmark nunca se ejecuta automáticamente, no llama a proveedores externos y limita la generación con `num_predict`.

Comandos útiles:

```text
Nova, prueba tu rendimiento.
Nova, ¿por qué Ollama tarda tanto?
Nova, rendimiento de Qwen de esta sesión.
Nova, muestra tu rendimiento de los últimos 15 minutos.
```

## Qué cambia en 0.9.2

0.9.2 corrige trabajo costoso innecesario detectado por Performance Profiler:

- preguntas deterministas como `Nova, ¿qué versión tienes?`, estado del sistema, top de procesos y workspace activo se resuelven antes de Semantic Memory/Ollama/Confidence/Expert;
- el contexto de memoria pasa a modo adaptativo: la recuperación léxica SQLite es la ruta barata por defecto y los embeddings se reservan para referencias a conversaciones previas, preferencias, continuidad o proyecto;
- si no existen recuerdos, `memory_search` no despierta Ollama para embeddings;
- el timeout del LLM local deja de reutilizar `internet.timeout_seconds`; el valor local por defecto es 45 s para evitar abortar inferencias válidas solo porque Internet esté configurado a 12 s;
- Fast Routing mantiene el turno en el historial local y Performance Profiler continúa midiendo su latencia, pero no genera una escalación experta innecesaria.

El objetivo es que una consulta de metadatos locales se mida en milisegundos/cientos de milisegundos, mientras que Qwen se reserve para consultas que realmente necesitan generación o razonamiento.

## Qué cambió en 0.9.0

Hasta 0.8.x, `agent.py`, `tools.py`, `ui.py` y `task_engine.py` seguían siendo archivos históricos presentes en la instalación local pero no recuperables desde el repositorio. Los módulos modernos de Nova se montaban encima mediante adaptadores estables.

0.9.0 elimina esa dependencia silenciosa:

- `assistant/agent.py` pasa a ser un `LocalAgent` administrado por GitHub;
- `assistant/tools.py` pasa a contener el contrato base de `LocalTools`;
- `assistant/ui.py` pasa a contener la UI base administrada;
- `assistant/task_engine.py` pasa a contener `TaskEngine`/`AutonomyEngine` administrado;
- CI valida que un checkout limpio pueda importar el core y montar los adaptadores de dominio;
- `architecture_status()` ya no declara archivos core locales/no administrados.

**Esto no significa que desaparecieron todos los adaptadores.** `agent_*`, `tools_*` y `ui_*` todavía amplían el core por dominio. La serie 0.9.x irá absorbiendo esas capas progresivamente sin mezclar migración de propiedad con una reescritura masiva de comportamiento.

## Capacidades preservadas durante la migración

### Browser Agent

Playwright mantiene un perfil persistente local en `data/browser_profile`. Las operaciones se ejecutan en un hilo dedicado para no compartir objetos sync de Playwright entre hilos de consultas diferentes.

Herramientas administradas: `browser_open`, `browser_search`, `browser_read`, `browser_inspect`, `browser_click`, `browser_fill`, `browser_press`, `browser_tabs`, `browser_back`, `browser_reload`.

Las acciones web sensibles siguen sujetas a la política de seguridad. Rellenar un campo no implica permiso para enviar el formulario.

### Escritorio

Nova conserva control estructurado por UI Automation y ventanas antes de recurrir a coordenadas:

- `window_list`, `window_activate`, `window_close`, `window_move`;
- `uia_snapshot`, `uia_click`, `uia_type`;
- fallback `mouse_move`, `mouse_click`, `keyboard_type`, `keyboard_press`.

### Voz

- hotkey global: **Ctrl + Alt + N**;
- contexto: **Ctrl + Alt + Shift + N**;
- push-to-talk: **F9**;
- STT local con faster-whisper;
- TTS local;
- wake word local mediante openWakeWord cuando el modelo configurado ya existe en el equipo.

0.9.0 **no descarga modelos de wake word automáticamente**. Si falta, Nova sigue funcionando con F9/hotkeys.

### Task Engine

El Task Engine nativo conserva Planner → Executor, persistencia en MemoryStore, pausa/reanudación/cancelación, reintentos, replanning acotado y límites de tiempo/tool-calls. Los planes no conceden permisos: los pasos siguen pasando por Agent/Tools y las reglas de seguridad.

## Evolución 0.8

### Skills + Reliability

Skills son playbooks declarativos, no scripts confiables. Una Skill nunca concede permisos por sí misma.

- **0.8.0 Skills Engine**: parámetros, pasos, verificaciones y scope global/workspace;
- **0.8.3 Learn from Expert**: una respuesta externa solo puede convertirse en candidata; requiere verificación positiva y la Skill aprendida nace `draft`;
- **0.8.4 Experience & Reliability**: reputación por versión (`unproven`, `learning`, `stable`, `watch`, `degraded`, `stale`) y revisión explícita ante degradación/obsolescencia.

### Confidence + Expert Escalation

Confidence Engine calcula un índice heurístico de respaldo usando evidencia estructurada. **No es una probabilidad calibrada** ni la autoconfianza del LLM.

Expert Escalation usa por defecto:

1. **Groq** — `openai/gpt-oss-120b`, cuando `GROQ_API_KEY` existe;
2. **Cerebras** — `gpt-oss-120b`, como fallback cuando está disponible;
3. **ChatGPT Assisted** — Nova prepara/sanitiza la consulta, abre ChatGPT y el usuario envía/copia la respuesta manualmente.

La API de OpenAI de pago está deshabilitada por defecto. ChatGPT Web no se usa como API: Nova no pulsa Enviar, no scrapea respuestas y no monitoriza el portapapeles continuamente.

Las claves solo se leen desde variables de entorno; nunca deben guardarse en Skills, repositorio o conversaciones.

## Percepción 0.7

- **0.7.0 Perception Engine**: metadatos de app/ventana y sistema, sin screenshots periódicos;
- **0.7.1 Context Intelligence**: relevancia y actividad probable;
- **0.7.2 Workspace Auto-Detection**: asociaciones app ↔ workspace, sin autoactivación por defecto;
- **0.7.3 Anomaly Detection**: baseline local y desviaciones sostenidas;
- **0.7.4 Event-driven Vision**: captura solo bajo petición o evento visual permitido.

## Memoria, Workspaces y continuidad

MemoryStore, Workspace Intelligence, Semantic Memory y Continuity son locales. Semantic Memory usa Ollama para embeddings y mantiene búsqueda léxica como fallback. Desde 0.9.2 el contexto automático usa recuperación semántica de forma adaptativa para no pagar el coste de embeddings en preguntas simples.

Desde 0.9.4 la UI puede precargar explícitamente el LLM principal mediante Warm Manager; esta operación es independiente de Semantic Memory y no ejecuta una consulta de usuario. Desde 0.9.5 Gaming Awareness puede suspender esa precarga temporalmente para priorizar un juego.

## Privacidad

No deben subirse: `config.json` real, `data/`, bases SQLite, perfil del navegador, screenshots, logs personales, `.venv/`, tokens ni API keys.

Perception no captura teclado/portapapeles/screenshots periódicamente. Event-driven Vision no conserva imágenes por defecto. Confidence, Expert Escalation, Learn from Expert y Skill Reliability guardan metadatos limitados y no el contenido completo que evalúan. LLM Performance Intelligence guarda únicamente métricas técnicas, conteos y muestras puntuales de recursos; nunca persiste el texto del prompt o de la respuesta. Instant Wake usa una petición local con `messages: []` y no transmite contenido a servicios externos. Gaming Awareness solo usa metadatos de ventana/proceso, rutas de ejecutables y telemetría local de GPU; no inspecciona memoria del juego ni inyecta código.

## Estructura 0.9

```text
nova/assistant/
├─ agent.py                 # core GitHub-managed + instrumentación Ollama
├─ agent_fast_routing.py    # rutas deterministas de baja latencia
├─ agent_instant_wake.py    # keep_alive + comandos Warm Manager
├─ agent_gaming.py          # comandos y contexto de Gaming Awareness
├─ llm_performance.py       # métricas locales detalladas de inferencia
├─ llm_benchmark.py         # benchmark explícito y acotado
├─ llm_warm.py              # precarga/descarga/estado/política runtime de Ollama
├─ gaming_awareness.py      # detección de juegos + política de VRAM
├─ perception_gaming.py     # throttle temporal de Perception
├─ hotkeys.py               # normalización y validación de atajos
├─ config_instant_wake.py   # defaults/migración 0.9.4
├─ config_gaming.py         # defaults/migración 0.9.5
├─ tools.py                 # core GitHub-managed
├─ ui.py                    # core GitHub-managed
├─ ui_instant_wake.py       # estado LLM + editor de hotkeys
├─ ui_gaming.py             # estado/ajustes de Gaming Awareness
├─ task_engine.py           # core GitHub-managed
├─ core_runtime.py          # bootstrap único
├─ tools_desktop.py         # Browser Agent + UIA/input administrados
├─ ui_voice_wake.py         # wake word local administrado
├─ memory.py
├─ perception.py
├─ skills.py
├─ confidence.py
├─ expert_escalation.py
├─ experience_reliability.py
├─ agent_*.py               # adaptadores temporales por dominio
├─ tools_*.py               # adaptadores/extensiones por dominio
└─ ui_*.py                  # adaptadores/extensiones visuales
```

## Desarrollo y publicación

Los PR ejecutan `compileall` y la suite completa de `unittest`. 0.9 añade smoke tests del core en checkout limpio. Al fusionar un nuevo `VERSION`, el workflow de publicación vuelve a validar la suite y crea la GitHub Release correspondiente.
