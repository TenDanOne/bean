# Arquitectura (resumen del CLAUDE.md original)

Detalle completo en `CLAUDE.md` (inglés). Pila: Django 5 + HTMX + Alpine + Tailwind 4; AGPL-3.0; sin build JS salvo Tailwind.

- **Tenancy:** Organization → Workspace → SocialAccount. `apps/members/middleware.py` (`RBACMiddleware`) adjunta org/workspace al request; decoradores en `apps/members/decorators.py`. Modelos base en `apps/common/managers.py`; el scoping es opt-in: usar siempre `for_org` / `for_workspace`.
- **Providers:** `providers/base.py` (`SocialProvider`), registro en `providers/__init__.py`, siempre `get_provider(platform, credentials)`. Credenciales: `resolve_platform_credentials` en `apps/credentials/models.py`; mapa env en `config/settings/base.py`. Ver [[Decisiones/Plataformas]].
- **Composer → publisher:** el estado vive en `PlatformPost`; `Post.status` se deriva en `apps/composer/status.py`. Motor: `apps/publisher/engine.py` (ciclo ~15 s, reintentos 60/300/1800 s).
- **Jobs:** django-background-tasks en la BD (sin Redis/Celery); recurrentes por `post_migrate`, keyed por `verbose_name`; preferir `register_recurring_task()` de `apps/common/background.py`. Ver [[Decisiones/Sin-worker]].
- **Settings en runtime:** `get_setting(workspace_id, key)` en `apps/settings_manager/helpers.py` (workspace → org → `APP_DEFAULTS`).
- **Cifrado:** `apps/common/encryption.py` (AES-256-GCM, clave de `SECRET_KEY` + `ENCRYPTION_KEY_SALT`); campos cifrados no se filtran.
- **API + MCP:** `/api/v1/` django-ninja (`apps/api/`), MCP en `apps/mcp/`, OAuth 2.1 en `apps/oauth_server/`.
- **Frontend:** `templates/` + HTMX + Alpine; helpers `apps/common/htmx.py`; Tailwind en `theme/static_src/`; CSP en `config/settings/base.py`. Ver [[Decisiones/Marca]].
- **Tests:** junto a cada app y `tests/`; requieren PostgreSQL (`config/settings/test.py`).
