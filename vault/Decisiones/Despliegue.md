# Decisión · Despliegue

- Railway, un solo servicio web (`railway.toml`, `Procfile`).
- BD: Supabase Postgres (pooler 6543 + directo 5432; pendiente verificar cuál usa migrate y cuál la app).
- Medios: Cloudflare R2 (`STORAGE_BACKEND=s3`, ya soportado).
- Dominio: `bean.ithos.mx`.
- Errores: Sentry opcional por `SENTRY_DSN`. Correo: MailerSend por SMTP.
- Sin secretos en el repo (gitleaks). Implementación: Fase 6.
