# Decisión · Despliegue

- Railway, un solo servicio web desde el `Dockerfile` (`railway.toml`). Autodeploy por push a `main` con «Wait for CI».
- `migrate` corre como `preDeployCommand` en `railway.toml` (con builder dockerfile, Railway ignora `release:` del `Procfile`). Healthcheck en `/health/`.
- Gunicorn `--timeout 120`: «Publicar ahora» publica dentro de la petición (subidas de video).
- BD: Supabase por **session pooler** (host del pooler, puerto 5432) para la app y para migrate. El transaction pooler (6543) pediría `DISABLE_SERVER_SIDE_CURSORS`/`CONN_MAX_AGE=0`; la conexión directa es solo IPv6.
- En Railway definir `DJANGO_SETTINGS_MODULE=config.settings.production` (si no, el `migrate` del pre-deploy usa development, que es el default de `manage.py`).
- `ALLOWED_HOSTS` debe incluir `healthcheck.railway.app` o el healthcheck da 400.
- Medios: Cloudflare R2 (`STORAGE_BACKEND=s3`, ya soportado).
- Dominio: `bean.ithos.mx`.
- Errores: Sentry opcional por `SENTRY_DSN`. Correo: MailerSend por SMTP.
- Sin secretos en el repo (gitleaks). `SECRET_KEY` y `ENCRYPTION_KEY_SALT` se generan una vez y no cambian.
