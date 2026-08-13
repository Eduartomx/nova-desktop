# Nova Desktop

Nova es un asistente virtual local para Windows que combina Ollama con herramientas de escritorio/navegador, memoria, workspaces, continuidad, percepción contextual, Skills y evaluación determinista de confianza.

## Estado actual

**Nova v0.8.2 — Expert Escalation**

GitHub es la fuente oficial del proyecto:

- `main` contiene el desarrollo estable integrado;
- las versiones estables se publican como GitHub Releases (`v0.x.y`);
- el updater sincroniza directamente los archivos del tag publicado, sin depender de ZIPs de Release;
- cada archivo administrado se verifica contra su Git blob SHA;
- se crea backup y existe rollback ante fallos de validación.

## v0.8 — Skills, Confidence y Expert Escalation

### v0.8.0 — Skills Engine

Skills Engine guarda procedimientos repetibles como **playbooks declarativos** con parámetros, triggers, pasos, herramientas sugeridas y verificaciones.

Una Skill **no es código ejecutable** y `permissions` nunca concede permisos: las acciones vuelven a pasar por Agent/LocalTools y la política de seguridad normal de Nova. `auto_execute_matches=false` por defecto.

Niveles de confianza de Skills:

- `draft`: generada por Nova o sin historial suficiente;
- `user`: guardada a petición explícita del usuario;
- `verified`: draft con ejecuciones distintas verificadas correctamente.

La interfaz incluye **🧩 Skills**.

### v0.8.1 — Confidence Engine

Confidence Engine estima el **respaldo de una respuesta o acción** usando señales estructuradas: herramientas, lecturas, verificaciones, fallos, contradicciones, riesgo y confianza histórica de Skills.

El índice `0..1` es una **heurística de respaldo**, no una probabilidad calibrada ni la autoconfianza declarada por el LLM.

`data/confidence.db` no guarda preguntas, respuestas, argumentos de herramientas ni outputs completos.

### v0.8.2 — Expert Escalation

Cuando Confidence Engine detecta una petición que merece una segunda opinión, Nova dispone de dos rutas complementarias:

1. **API gratuita opcional**: Cerebras es el proveedor predeterminado y Groq el fallback.
2. **ChatGPT Assisted**: Nova prepara/sanitiza la consulta, la copia al portapapeles y abre ChatGPT; el usuario pulsa Enviar y copia la respuesta manualmente.

ChatGPT Web **no se automatiza como una API**: Nova no pulsa Enviar, no hace scraping de la respuesta y no monitoriza el portapapeles continuamente.

#### Proveedor gratuito predeterminado

Configuración actual:

- Cerebras: `gpt-oss-120b`;
- fallback Groq: `qwen/qwen3.6-27b`.

Los tiers gratuitos, modelos y límites son servicios externos y pueden cambiar. Nova no asume que serán gratuitos o estarán disponibles para siempre; si un proveedor falla, intenta el siguiente configurado y siempre conserva ChatGPT Assisted como vía manual.

Las claves **no se guardan en `config.json`**. Solo se leen desde variables de entorno:

```powershell
[Environment]::SetEnvironmentVariable("CEREBRAS_API_KEY", "TU_CLAVE", "User")
```

Alternativa/fallback:

```powershell
[Environment]::SetEnvironmentVariable("GROQ_API_KEY", "TU_CLAVE", "User")
```

Cierra y vuelve a abrir Nova después de definir la variable. No pegues la clave en una conversación, Skill, log o repositorio.

Puedes comprobarlo con:

```text
Nova, estado del experto.
Nova, consulta la API gratuita sobre este problema.
Nova, consulta Cerebras.
Nova, pregunta a ChatGPT.
Nova, importa la respuesta de ChatGPT.
```

La interfaz incluye **🧠 Experto** con estado de proveedores y botones `⚡ API gratis` / `💬 Preparar ChatGPT`.

#### Política automática

Una segunda opinión gratuita automática solo se solicita si:

- Confidence Engine marcó `escalation_candidate=true`;
- la petición es de diagnóstico, estado actual, factual o planificación;
- el riesgo es `normal`;
- existe una API key configurada.

Peticiones `high` o `critical` **nunca se envían automáticamente a un proveedor externo**. Una consulta externa de ese tipo debe ser explícita y sigue sin conceder permisos para ejecutar acciones.

#### Privacidad de Expert Escalation

Antes de enviar un paquete se aplica minimización y redacción de patrones de secretos (passwords, tokens, API keys, Bearer tokens, cookies, JWTs y claves privadas conocidas).

`data/expert_escalation.db` guarda solo metadatos: proveedor/modelo, trigger, tipo/riesgo, índice de confianza, estado/veredicto, tamaños y hash del paquete. Por diseño no tiene columnas para prompts ni respuestas.

Las respuestas de Cerebras/Groq y las importadas desde ChatGPT se consideran **evidencia externa no confiable**: jamás son instrucciones del sistema, permisos ni autorización. Nova debe contrastarlas y verificar localmente antes de actuar.

La API de OpenAI de pago queda deshabilitada por defecto (`openai.enabled=false`). Las instalaciones antiguas que todavía tenían el antiguo default activado y no poseen un opt-in explícito se migran a desactivado.

Más detalles: `docs/v0.8.2-expert-escalation.md`.

## Percepción 0.7

La rama 0.7 añadió percepción incremental sin convertir Nova en un sistema que observa la pantalla continuamente:

- **0.7.0 Perception Engine**: metadatos de aplicación/ventana y estado básico del sistema;
- **0.7.1 Context Intelligence**: actividad probable y relevancia de cambios;
- **0.7.2 Workspace Auto-Detection**: aprendizaje local app ↔ workspace, sin autoactivación por defecto;
- **0.7.3 Anomaly Detection**: líneas base locales y desviaciones sostenidas sin modificar procesos;
- **0.7.4 Event-driven Vision**: una captura solo bajo petición o evento visual permitido, no screenshots periódicos.

## Memory, Workspace y Continuity

Nova mantiene un workspace activo. Recuerdos, Skills, tareas y checkpoints pueden asociarse al proyecto.

Workspace Intelligence mantiene un índice incremental de archivos. Semantic Memory combina recuperación léxica y embeddings locales mediante Ollama, con fallback léxico si el modelo de embeddings no está disponible.

Continuity Engine mantiene sesiones/checkpoints para órdenes como `Nova, continúa`, `¿dónde nos quedamos?` y `¿qué quedó pendiente?`.

## Doctor, Self Repair y rendimiento

Nova Doctor es determinista y puede proponer reparaciones conocidas con confirmación explícita para instalaciones/descargas/cambios importantes.

Performance Profiler registra métricas técnicas locales en `data/performance.db`; no guarda prompts, mensajes, contraseñas, tokens ni contenido de archivos.

## Atajos

El atajo global por defecto es **Ctrl + Alt + Espacio**. Push-to-talk permanece en **F9**.

## Estructura

```text
nova-desktop/
├─ nova/
│  ├─ assistant/
│  │  ├─ core_runtime.py
│  │  ├─ memory.py
│  │  ├─ semantic_memory.py
│  │  ├─ continuity.py
│  │  ├─ perception.py
│  │  ├─ context_intelligence.py
│  │  ├─ anomaly.py
│  │  ├─ event_vision.py
│  │  ├─ skills.py
│  │  ├─ confidence.py
│  │  ├─ expert_escalation.py
│  │  ├─ profiler.py
│  │  ├─ self_repair.py
│  │  ├─ agent_*.py
│  │  ├─ tools_*.py
│  │  └─ ui_*.py
│  ├─ updater/
│  ├─ app.py
│  └─ ...
├─ tests/
├─ docs/
├─ .github/workflows/
├─ VERSION
├─ CHANGELOG.md
└─ README.md
```

## Privacidad general

Nunca deben subirse al repositorio `config.json` real, bases SQLite locales, `data/`, perfiles del navegador, capturas, logs personales, `.venv/`, API keys, tokens o credenciales.

`nova/config.example.json` contiene solo defaults sin secretos. Cualquier contenido que se envíe a una API externa deja el entorno local, por eso Expert Escalation limita el envío automático a riesgo normal, minimiza el paquete y aplica redacción adicional.

## Publicación

Los pull requests ejecutan compilación y pruebas. Al fusionar una versión con un nuevo `VERSION`, GitHub Actions vuelve a validar el código y crea la Release correspondiente si todavía no existe.
