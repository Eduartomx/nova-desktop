# Nova Desktop

Nova es un asistente virtual local para Windows que combina un modelo local mediante Ollama con herramientas de escritorio, navegador, memoria y automatización de tareas.

## Estado actual

**Nova v0.5.8 — GitHub Native Updates**

Desde v0.5.8, GitHub es la fuente oficial del proyecto:

- el código instalable vive en `nova/`;
- `main` contiene el desarrollo integrado actual;
- las versiones estables se marcan con GitHub Releases (`v0.x.y`);
- el updater obtiene directamente los archivos del tag publicado, sin ZIPs;
- cada archivo se verifica contra su Git blob SHA antes de reemplazar la copia local;
- Nova hace backup y rollback si la validación posterior falla.

## Estructura

```text
nova-desktop/
├─ nova/                    # código que se sincroniza con la instalación local
│  ├─ assistant/
│  ├─ updater/
│  ├─ app.py
│  ├─ requirements.txt
│  └─ ...
├─ .github/workflows/       # CI y publicación de releases
├─ VERSION                  # versión que se publicará
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

Al cambiar `VERSION` y hacer push a `main`, GitHub Actions valida la sintaxis Python y crea la Release correspondiente si aún no existe. La release apunta al commit exacto y no necesita un paquete ZIP.
