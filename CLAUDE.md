# CLAUDE.md

## BEAN by ITHOS — reglas del proyecto (leer primero)

Fork personal y no comercial de BrightBean Studio. Esta sección va en español; el resto del archivo se conserva en inglés tal cual.

- **Antes de abrir código:** leer `vault/00-Indice.md` (y `graphify-out/GRAPH_REPORT.md` si existe). **Al cerrar fase:** actualizar `HANDOFF.md`.
- **Nombre:** «BEAN by ITHOS» (en la UI: BEAN). Uso personal, un solo usuario por ahora; roles/RBAC se conservan intactos.
- **Plataformas activas:** YouTube, Instagram (vía Facebook/Meta Graph), Facebook, TikTok. Las demás se OCULTAN con `DEFAULT_ENABLED_PLATFORMS` de `apps/social_accounts/platforms.py` (override por env `BBS_ENABLED_PLATFORMS`), nunca se borran (choices, migraciones y providers intactos).
- **Sin worker 24/7:** modo MANUAL (botón «Procesar pendientes» que drena la cola de django-background-tasks) + `POST /cron/run` con Bearer `CRON_SECRET`, dormido (503 sin secreto, 401 con bearer malo). Un post programado sale cuando el usuario procesa.
- **Despliegue:** Railway (un solo servicio web); BD Supabase (pooler 6543 + directo 5432); medios en Cloudflare R2 (`STORAGE_BACKEND=s3`); dominio `bean.ithos.mx`; Sentry opcional por `SENTRY_DSN`; correo MailerSend por SMTP.
- **Marca:** estilo ITHOS (kit de SIMU, isotipo ITHOS, Figtree) con paleta propia que combina los colores originales de BrightBean con los de ITHOS, como agradecimiento indirecto. Paleta final pendiente de elección del usuario. No usar logos de BrightBean.
- **AGPL:** repo público GitHub `TenDanOne/bean`, `LICENSE` intacto, aviso «BEAN by ITHOS está basado en BrightBean Studio (AGPL-3.0)» con enlace al código.
- **App Review:** Meta, TikTok y Google (privacidad, términos, borrado de datos, demos).
- **Regla de oro:** no romper funciones existentes. Cada fase cierra con `pytest` + `ruff` + `mypy` contra la línea base `docs/linea-base-2026-10-17.md`.
- **Roles de agentes:** Haiku lee (`lector-vault`), Sonnet hace (`implementador`) y revisa a Haiku (`revisor`), Opus juzga (`juez-bean`; `design-system-bean` para lo visual).
- **Hermanos de referencia (solo leer):** `C:\Users\ultra\Proyectos\simu` y `C:\Users\ultra\Proyectos\paax`.

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

BrightBean Studio — a self-hostable social media management platform (Django 5 + HTMX + Alpine + Tailwind 4). Agencies manage organizations → workspaces → connected social accounts, compose and approve posts, publish them to ~13 platforms via a background worker, and read back analytics/inbox. AGPL-3.0.

There is no JS build step for the app itself; only Tailwind compiles.

## Commands

```bash
# Setup (or: make setup)
cp .env.example .env
pip install -r requirements.txt
cd theme/static_src && npm install && cd ../..
python manage.py migrate

# Run — needs 3 terminals
python manage.py runserver          # make server
python manage.py process_tasks      # make worker — REQUIRED, nothing publishes without it
cd theme/static_src && npm run start # make tailwind

# Tests (Postgres required — see gotcha below)
pytest
pytest apps/composer/tests/test_views.py                      # one file
pytest apps/composer/tests/test_views.py::TestCompose::test_x  # one test
pytest -k "schedule and not media"                            # by name
pytest --cov=apps --cov-report=term-missing                   # make test-cov

# Quality — CI runs exactly these
ruff check . && ruff format --check .                              # make lint
ruff check --fix . && ruff format .                                # make format
mypy apps/ config/ providers/ tests/ --ignore-missing-imports      # make typecheck

# Docker
docker compose up -d                                               # make docker-up
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

`make help` lists every target. Pre-commit hooks (`pre-commit install` + `--hook-type pre-push`) enforce ruff, mypy, gitleaks, and `manage.py check`.

### Environment gotchas

- **Tests need PostgreSQL.** [config/settings/test.py](config/settings/test.py) hardcodes the postgres engine and the `brightbean_test` database (`DB_HOST`/`DB_USER`/`DB_PASSWORD` env overrides). Local *dev* may run on SQLite (`DATABASE_URL=sqlite:///db.sqlite3`), but pytest will not.
- `DJANGO_SETTINGS_MODULE` defaults differ by entrypoint: `manage.py` → `development`, `wsgi.py`/`asgi.py` → `production`, pytest → `test` (set in `pyproject.toml`).
- `SECRET_KEY` and `ENCRYPTION_KEY_SALT` must exist or the app won't boot / can't decrypt. Changing either orphans every encrypted token in the DB.
- Ruff: line-length 120, `select = E,F,I,N,W,UP,B,SIM`, migrations excluded. Per-file ignores in `pyproject.toml` are deliberate — read the comment before adding another.

## Architecture

### Tenancy and request context

`Organization → Workspace → SocialAccount` is the spine. Most URLs are `/workspace/<uuid:workspace_id>/...`.

`apps.members.middleware.RBACMiddleware` runs on every request and attaches `request.org`, `request.org_membership`, `request.workspace`, `request.workspace_membership`. Workspace resolution happens in `process_view` (needs `resolver_match`), falling back to `user.last_workspace_id` for global pages. Views enforce access with the decorators in [apps/members/decorators.py](apps/members/decorators.py): `require_org_role`, `require_workspace_role`, `require_permission`. Org roles are owner/admin/member; workspace roles are owner > manager > editor > contributor > client > viewer, plus custom roles resolved through `effective_permissions`.

Models inherit `OrgScopedModel`/`WorkspaceScopedModel` from [apps/common/managers.py](apps/common/managers.py), which add the FK plus a `.for_org(id)` / `.for_workspace(id)` manager method. Scoping is **opt-in per query** — there is no global filter, so a bare `.objects.filter(...)` leaks across tenants. Always go through `for_org` / `for_workspace`.

### Providers (`providers/`)

One file per platform, each subclassing `SocialProvider` ([providers/base.py](providers/base.py)) and registered in `PROVIDER_REGISTRY` ([providers/__init__.py](providers/__init__.py)). Call sites only ever do `get_provider(platform, credentials)` — publisher, inbox, and analytics contain no platform-specific branches.

The base class distinguishes **abstract** (`get_profile`, `publish_post`, plus metadata properties like `max_caption_length`, `supported_post_types`) from **optional** methods that raise `NotImplementedError` by default (analytics, inbox, comments, OAuth). Capability flags on the class steer the surrounding machinery: `uses_pkce`, `analytics_only_scopes` (omitted from the OAuth request when a platform's analytics is disabled, so unapproved apps can still publish), `account_metrics_supports_date_range`. Use `self._request(...)` for HTTP — it maps 429 → `RateLimitError` and 4xx/5xx → `APIError`.

Adding a platform: new file in `providers/`, register it, add credentials to `PLATFORM_CREDENTIALS_FROM_ENV` in [config/settings/base.py](config/settings/base.py) and `.env.example`, add platform choices/connect flow, tests in `tests/providers/`, README setup section. See `CONTRIBUTING.md`.

Credentials resolve via `resolve_platform_credentials(platform, org_id)` in [apps/credentials/models.py](apps/credentials/models.py): `.env` wins **only if it fully satisfies the platform's required keys**, otherwise the org's admin-entered `PlatformCredential` row. Several platforms deliberately share one app (Facebook/Instagram/Threads → Meta; YouTube/Google Business → Google), and LinkedIn derives an `_oauth_mode` at settings load depending on which env vars are set.

### Composer → approvals → publisher

Editorial status lives on **`PlatformPost`** (one row per target account), not on `Post`. `Post.status` is *derived* by `derive_post_status()` in [apps/composer/status.py](apps/composer/status.py), which returns the "lowest" (most action-required) child status, with special cases for terminal mixes (`partially_published`) and `on_hold`. Never write a status onto `Post`; change the children.

[apps/publisher/engine.py](apps/publisher/engine.py) polls `PlatformPost`s due for publish every ~15s, groups them by parent post, and dispatches via a thread pool (`MAX_CONCURRENT_POSTS` × `MAX_CONCURRENT_PUBLISHES`). Retries use the fixed `RETRY_BACKOFF = [60, 300, 1800]`; first comments post after a delay. Results land in `PublishLog` / `RateLimitState`.

### Background jobs

`django-background-tasks` on the app database — **no Redis or Celery**. Recurring jobs are registered idempotently from each `AppConfig.ready()` via a `post_migrate` signal (see [apps/publisher/apps.py](apps/publisher/apps.py), [apps/analytics/apps.py](apps/analytics/apps.py)); newer code should use `register_recurring_task()` from [apps/common/background.py](apps/common/background.py). Consequences:

- A task only starts existing after a `migrate` runs, and it is keyed by `verbose_name` — renaming that string creates a duplicate schedule rather than updating the old one.
- Changing an interval constant is **only** picked up by the apps that go through `register_recurring_task()`, which reconciles a changed `repeat` on the existing row (and pulls `run_at` in when the cadence shrinks). `publisher`, `analytics`, `social_accounts`, and `api` still carry inline copies of the pattern that only create-if-missing, so editing their intervals is silently inert until the row is deleted.
- Run `migrate` with the worker **stopped**. `create_repetition()` runs after the task body using the `Task` row loaded at the start, so reconciling mid-flight leaves the new row with the old cadence until the next `migrate`.
- Nothing prunes `CompletedTask`. `run_publish_cycle` alone writes ~5,760 rows/day at its 15s cadence, and there are no `BACKGROUND_TASK_*` settings configured.

### Settings cascade (runtime config, not Django settings)

`get_setting(workspace_id, key)` in [apps/settings_manager/helpers.py](apps/settings_manager/helpers.py) resolves workspace override → org override → `APP_DEFAULTS`. Tunables (approval reminders, sync intervals, publishing behaviour) belong here, not hardcoded in views.

### Encryption

OAuth tokens, API keys, and credentials use `EncryptedTextField` / `EncryptedJSONField` ([apps/common/encryption.py](apps/common/encryption.py)) — AES-256-GCM with the key derived from `SECRET_KEY` + `ENCRYPTION_KEY_SALT` via HKDF. Encrypted columns are opaque TextFields: you cannot filter, index, or `__contains` on them.

### Agent API + MCP

`/api/v1/` is django-ninja ([apps/api/](apps/api/), routers in `apps/api/routers/`), authenticated by `bb_studio_…` bearer keys. [apps/api/auth.py](apps/api/auth.py) builds a duck-typed `VirtualMembership` so the same `@require_permission` decorators used by HTML views work inside Ninja routes, and it re-intersects the key's grants with the issuer's *current* workspace permissions on every request (demotions take effect immediately, no job needed). Permission keys: `create_posts`, `publish_directly`, `upload_media`, `view_analytics`. Writes accept `idempotency_key`.

`POST /api/v1/mcp` speaks JSON-RPC 2.0 ([apps/mcp/](apps/mcp/): `protocol.py`, `transport.py`, `handlers.py`, `tools.py`). It supports both static API keys and a full OAuth 2.1 flow for native connectors — [apps/oauth_server/](apps/oauth_server/) adds Dynamic Client Registration and the `.well-known` discovery documents on top of django-oauth-toolkit, with PKCE `S256` enforced by a custom validator.

### Optional modules

Optional integrations must be gated at settings and URL registration time, with their migrations and templates kept isolated from the core app. Do not document a module here until its package, settings gate, URLs, and tests exist in the current tree.

### Frontend

Server-rendered templates in `templates/` (mirroring app names) with HTMX for partials/forms/polling and Alpine.js for local UI state. Return partials from HTMX requests; use `trigger_response` / `toast_response` from [apps/common/htmx.py](apps/common/htmx.py) to fire `HX-Trigger` events (e.g. `showToast` + a refresh event) instead of re-rendering a page. Tailwind lives in `theme/static_src/`; white-label brand colors are injected as CSS custom properties at runtime, so per-org theming never requires a rebuild. CSP is enforced in production (`report-only` in dev/test) — adding an external asset host means updating the `CSP_*` tuples in `base.py`.

## Testing conventions

Tests live beside their app (`apps/<app>/tests/` or `apps/<app>/tests.py`); `tests/` at the root holds cross-cutting and provider tests. Shared fixtures (`user`, `organization`, `org_owner`) are in [conftest.py](conftest.py). Provider tests mock HTTP — never hit real platform APIs.
