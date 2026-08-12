# Nova Desktop

Nova es un asistente virtual local para Windows que combina un modelo local mediante Ollama con herramientas de escritorio, navegador, memoria y automatización de tareas.

## Estado actual

**Nova v0.6.0 — Memory & Workspace**

GitHub es la fuente oficial del proyecto:

- el código administrado por el updater vive en `nova/`;
- `main` contiene el desarrollo estable integrado;
- las versiones estables se publican como GitHub Releases (`v0.x.y`);
- el updater sincroniza directamente los archivos del tag publicado, sin ZIPs;
- cada archivo se verifica contra su Git blob SHA;
- se crea un backup antes de reemplazar archivos y existe rollback ante fallos de validación.

## v0.6.0 — Memory & Workspace

Nova puede mantener un **workspace activo** que representa el proyecto con el que estás trabajando. Guarda ruta, tipo de proyecto, metadatos básicos, memorias asociadas y continuidad de tareas.

Ejemplos:

- `mi servidor` puede referirse al workspace activo del servidor Minecraft;
- una tarea creada mientras un workspace está activo queda asociada a ese proyecto;
- Nova recupera solo las memorias relevantes para la petición actual, reduciendo contexto y latencia;
- las rutas de workspaces registrados pasan a ser raíces de trabajo reconocidas por Nova.

Tipos detectados inicialmente: Nova, servidor Minecraft, Python, Node, Arduino, Godot, Unity, Visual Studio, Git y genérico.

La interfaz incorpora:

- `📁 Proyectos` para registrar y cambiar de workspace;
- `🩺 Doctor` para revisar componentes sin gastar una inferencia del LLM;
- `⬆ Actualizar` para instalar la siguiente Release desde GitHub y reiniciar Nova si termina correctamente.

## Migración del núcleo

v0.6.0 usa una **capa de compatibilidad administrada desde GitHub** (`v060_*`) sobre los módulos históricos v0.5 que ya están instalados localmente. Esto permite añadir Memory & Workspace sin reemplazar de golpe archivos grandes todavía no migrados al repositorio. La migración completa del núcleo continuará en versiones posteriores.

## Estructura

```text
nova-desktop/
├─ nova/
│  ├─ assistant/
│  │  ├─ memory.py           # base histórica estable
│  │  ├─ workspace.py        # workspaces v0.6
│  │  ├─ doctor.py           # diagnóstico rápido
│  │  ├─ v060_memory.py      # extensión de memoria/SQLite
│  │  ├─ v060_tools.py
│  │  ├─ v060_agent.py
│  │  ├─ v060_ui.py
│  │  └─ v060_runtime.py
│  ├─ updater/
│  ├─ app.py
│  └─ ...
├─ tests/
├─ .github/workflows/
├─ VERSION
├─ CHANGELOG.md
└─ README.md
```

## Privacidad

Nunca deben subirse al repositorio:

- `config.json` real;
- `assistant.db` o bases de memoria;
- `data/`;
- perfiles del navegador;
- capturas o logs personales;
- `.venv/`;
- claves API, tokens o credenciales.

`nova/config.example.json` contiene únicamente valores de ejemplo/por defecto.

## Publicación

Los pull requests ejecutan compilación y pruebas de Memory/Workspace. Al fusionar una versión con un nuevo `VERSION`, GitHub Actions vuelve a validar el código y crea la Release correspondiente si todavía no existe.
