# Decisión · Despliegue

- Railway, un solo servicio web desde el `Dockerfile` (autodetectado). Sin `railway.toml`: Config as Code está deprecado y no se puede activar en servicios nuevos; healthcheck y reinicios se configuran en la UI. Autodeploy por push a `main` con «Wait for CI».
- `migrate` corre en el CMD del `Dockerfile` antes de gunicorn. No hay `Procfile`: Railway lo leía sin shell y `$PORT` no se expandía. Healthcheck `/health/` puesto en la UI de Railway.
- Gunicorn `--timeout 120`: «Publicar ahora» publica dentro de la petición (subidas de video).
- BD: Supabase por **session pooler** (host del pooler, puerto 5432) para la app y para migrate. El transaction pooler (6543) pediría `DISABLE_SERVER_SIDE_CURSORS`/`CONN_MAX_AGE=0`; la conexión directa es solo IPv6.
- En Railway definir `DJANGO_SETTINGS_MODULE=config.settings.production` (si no, el `migrate` del pre-deploy usa development, que es el default de `manage.py`).
- `ALLOWED_HOSTS` debe incluir `healthcheck.railway.app` o el healthcheck da 400.
- Medios: Cloudflare R2 (`STORAGE_BACKEND=s3`, ya soportado).
- Dominio: `bean.ithos.mx`.
- Errores: Sentry opcional por `SENTRY_DSN`. Correo: MailerSend por SMTP.
- Sin secretos en el repo (gitleaks). `SECRET_KEY` y `ENCRYPTION_KEY_SALT` se generan una vez y no cambian.
