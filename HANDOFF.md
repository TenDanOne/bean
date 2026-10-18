# HANDOFF — BEAN by ITHOS

## Estado (2026-10-17): Fase 0 hecha, Fase 1 en curso
- Fase 0: copia de BrightBean Studio como base, commit `6585f53`, `.gitattributes` con LF.
- Fase 1 «forma de trabajo» (estilo SIMU/PAAX): `CLAUDE.md` con sección BEAN arriba, `vault/`, este `HANDOFF.md`, `.claude/hooks/validate-bash.{sh,mjs}` (adaptado a Django: bloquea `flush`/`reset_db`/`migrate <app> zero`/DROP), `.claude/settings.json` (permisos + hook; `launch.json` conservado) y `.claude/agents/` (`lector-vault`, `juez-bean`, `design-system-bean`). Sin commit todavía.
- Línea base de pruebas: `docs/linea-base-2026-10-17.md` (la crea otro agente).

## Fases siguientes
2. Ocultar plataformas con `DEFAULT_ENABLED_PLATFORMS` (`apps/social_accounts/platforms.py`, override `BBS_ENABLED_PLATFORMS`) (YouTube, Instagram, Facebook, TikTok activas; el resto oculto, no borrado).
3. Sin worker: botón «Procesar pendientes» + `POST /cron/run` (Bearer `CRON_SECRET`, 503 sin secreto, 401 bearer malo).
4. Marca: estilo ITHOS (isotipo, Figtree, paleta combinada BrightBean + ITHOS).
5. Kit legal + App Review (Meta, TikTok, Google): privacidad, términos, borrado de datos, demos.
6. Sentry, correo (MailerSend SMTP), Railway, Supabase, R2, GitHub `TenDanOne/bean`, dominio `bean.ithos.mx`.

## Pendiente / dudas
- Paleta final: la elige el usuario.
- Verificar requisitos exactos de App Review por plataforma (marcados «pendiente verificar» en `vault/Plataformas/`).
- Los tests requieren PostgreSQL (`brightbean_test`).

## RETOMAR AQUÍ
- Pasar `juez-bean` sobre la Fase 1 (archivos de arriba), hacer commit y empezar la Fase 2.

## Verificación
```bash
git status --short
pytest && ruff check . && ruff format --check . && mypy apps/ config/ providers/ tests/ --ignore-missing-imports
```

---

## RETOMAR AQUÍ — 2026-10-17 (cierre de sesión en OneDrive)

**Desde ahora trabajar abriendo Claude en `C:\Users\ultra\Proyectos\bean`.** El original en `OneDrive\Escritorio\bbs_unxio-main` ya no se toca. Borrarlo cuando el usuario confirme.

**Commits:** `6585f53` original · `4d121bf` fase 1 · `6fbcb45` fases 2+3 · `629236c` fase 4.3. Juez: todas PASA (con pendientes).

**Verificación al cierre:**
- pytest: 1497 passed / 2 failed preexistentes (`test_preview` encoding cp1252, `test_channel_scoping` bulk_assign).
- ruff check OK; mypy con 4 errores preexistentes.
- Credenciales PG de tests: DATABASE_URL de `simu/.env` → env DB_USER/DB_PASSWORD (no escribirlas).
- No correr dos pytest en paralelo.

**Hecho:**
- 4 plataformas (`platforms.py`, env `BBS_ENABLED_PLATFORMS`).
- Sin worker:
  - `drain_pending_tasks` (`apps/common/background.py`).
  - Botón "Procesar pendientes" en ajustes de la organización (`POST /tasks/process/`).
  - `POST /cron/run` con `CRON_SECRET`.
  - Publicar ahora drena la cola.
- Marca: paleta A Oliva y petróleo, Figtree local, logo/favicon ITHOS, BEAN en templates, NOTICE + pie AGPL.

**Pendientes del juez (siguiente sesión, en este orden):**
1. Publicar ahora: correr primero `run_publish_cycle` (o darle `priority`) dentro del drain, para que no espere detrás de syncs lentos (`apps/common/background.py`, `apps/composer/views.py:1095`).
2. Botón "Procesar pendientes" visible solo para owner/admin (`templates/organizations/settings.html:229`); con más orgs, restringir a `is_superuser` (`apps/common/views.py:17`).
3. Avisos que usaban naranja ahora salen oliva: pasar a `amber-*` en `templates/base.html:716,733` y `templates/approvals/partials/_approval_row.html:39`.
4. `styles.css:61`: `--primary-soft` fijo → `var(--brand-100)`.
5. Figtree solo trae Regular + ExtraBold: añadir SemiBold/Bold (o woff2 variable).
6. Restos de marca:
   - Textos: `templates/calendar/calendar.html:4` (title), `apps/members/services.py:616` (asunto), título en `apps/api/api.py`, README `.github/assets`.
   - Colores inline en `calendar/partials/publish_calendar_shell.html:140` y `event_form.html:49`.
   - 403/404/500 con variables no definidas.
   - Logo en sidebar colapsada (`base.html:309`).
7. Comentario de `BB_TRUSTED_PROXIES` separado en `config/settings/base.py:157-161`; `ruff format` de `scripts/marca/*.py`.
8. Modo oscuro (no existía en el original). Hex oscuros en `docs/marca/propuestas-paleta.html` §A; logo `static/marca/bean-by-ithos-oscuro.svg`.
9. Decidir idioma de la UI (hoy en inglés).

**Después:**
- Fase 5: releer y reescribir legales `templates/public/*`, `/legal/data-deletion/`, `docs/APP-REVIEW.md`.
- Fase 6: Sentry, MailerSend SMTP, Supabase, R2, Railway, timeout de gunicorn/proxy > 25 s, repo público TenDanOne/bean (push solo con confirmación).
