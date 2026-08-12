# Changelog

## v0.5.8 — GitHub Native Updates

- GitHub pasa a ser la fuente de verdad del código de Nova.
- El código instalable se publica bajo `nova/`.
- El updater deja de depender de ZIPs y sincroniza archivos directamente desde el tag estable.
- Las consultas de Releases usan GitHub CLI autenticado para evitar el rate limit anónimo.
- Cada archivo descargado se valida contra su Git blob SHA.
- Backup y rollback automático antes de reemplazar archivos.
- Actualización de dependencias solo cuando cambia `requirements.txt`.
- Validación de sintaxis Python después de actualizar.

## v0.5.7 — GitHub Update Infrastructure

- Primer repositorio y Release oficial de Nova.
- Integración inicial con GitHub Releases.
