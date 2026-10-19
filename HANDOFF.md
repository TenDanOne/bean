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

**Sesión 2026-10-17 (b) — preparar Railway:**
- Hecho pendiente 1: «Publicar ahora» llama `PublishEngine().poll_and_publish()` antes del drain (`apps/composer/views.py`). Tests de `test_account_scope.py` lo mockean.
- Reintentos (`apps/publisher/engine.py` `_process_retries`) ahora reservan la fila con un `update()` atómico, para que publicar-ahora + drain no publiquen dos veces (hallazgo del juez).
- Pendiente 2 resuelto sin código: la página de ajustes ya exige ADMIN (`apps/organizations/views.py:49`).
- `railway.toml`: `preDeployCommand` migrate + healthcheck `/health/`. Gunicorn `--timeout 120` (`Dockerfile`, `Procfile`).
- Ver `vault/Decisiones/Despliegue.md` (session pooler, ALLOWED_HOSTS con `healthcheck.railway.app`).
- Multi-red ya existe: un `Post` → un `PlatformPost` por cuenta con `platform_specific_*`; `clone_post` para reutilizar.
- **Falta correr pytest completo** (no hubo acceso a las credenciales de PG en esa sesión). ruff, `manage.py check` y mypy (sin errores nuevos) OK.

**Sesión 2026-10-18 — BEAN vivo en Railway:**
- Repo público https://github.com/TenDanOne/bean; CI verde (commit `72ed30e`: formato, mypy 0 errores, test bulk assign corregido).
- Railway: servicio `web` en `web-production-70790.up.railway.app`; variables como Shared Variables (`${{shared.X}}`). Secretos en `C:\Users\ultra\bean-railway.env` (fuera del repo).
- Lecciones: Railway tomó el `Procfile` sin shell (`$PORT` literal) → Procfile borrado. Config as Code deprecado → `railway.toml` borrado. El `preDeployCommand` nunca corrió → migrate en el CMD del Dockerfile, pero el Custom Start Command de la UI lo pisa: debe ser `sh -c 'python manage.py migrate --noinput && gunicorn ...'`.
- Migraciones aplicadas a mano desde local contra Supabase (session pooler) con un script del scratchpad que lee `bean-railway.env` sin imprimirlo. Site id=1 → `bean.ithos.mx` / `BEAN`.
- Login y registro responden 200. Registro abierto (decisión del usuario).
- Pendientes: confirmar el Start Command con migrate en la UI; Healthcheck `/health/` en la UI; CNAME `bean.ithos.mx`; «Wait for CI»; cron externo opcional (cron-job.org → `POST /cron/run`); README/architecture aún mencionan Procfile/railway.toml (docs del upstream).

**Después:**
- Fase 5: releer y reescribir legales `templates/public/*`, `/legal/data-deletion/`, `docs/APP-REVIEW.md`.
- Fase 6: Sentry, MailerSend SMTP, Supabase, R2, Railway, timeout de gunicorn/proxy > 25 s, repo público TenDanOne/bean (push solo con confirmación).

## RETOMAR AQUÍ — 2026-10-18 (Fase 5: listo para App Review, primera mitad)
Plan: `~/.claude/plans/bien-ya-qued-desplegada-twinkly-possum.md`. Juez: PASA, sin regresiones.
- Hecho:
  - Paso 1: el grid de connect y el sidebar filtran por `enabled_platform_keys()`.
  - Paso 2A: `LANGUAGE_CODE=es-mx`; base.html, social_accounts y onboarding en español.
  - Paso 4: landing en `/` para anónimos; legales ES/EN; `/legal/data-deletion/` con sus callbacks de Meta (`/legal/data-deletion/callback/`, `/legal/meta/deauthorize/`); env `BEAN_CONTACT_EMAIL`.
  - Paso 5: `docs/producto/*`, `docs/GUIA-USO.md`, `docs/APP-REVIEW.md` (brechas M1-M7, T1-T7), `docs/marca/manual.html`.
- Railway necesita `PLATFORM_FACEBOOK_APP_SECRET` o los callbacks de Meta responden 503.
- Siguiente: brechas de TikTok T1-T5 (docs/APP-REVIEW.md). Después: el resto del español (calendario, composer, aprobaciones, inbox, ajustes), el paso 3 (pulido visual), M1 (recortar scopes de Meta) y M5 (guardar el user id de Meta al conectar).
- Tests locales: Postgres 17 con la credencial local de simu vía DB_USER/DB_PASSWORD (sin guardarla).

### 2026-10-18 (cont.) — TikTok y español completo
- TikTok:
  - Hecho: T1 (abortar si falla creator_info; 401/403 sin reintento), T2 parcial (aviso de duración máxima), T3 (ya existía), T4 (consentimiento junto al botón).
  - Pendiente: T5 «procesando→publicado», que necesita un estado en PlatformPost, una migración y sondeo en el engine.
- UI completa en español sin i18n:
  - Etiquetas de choices traducidas con 6 migraciones de solo AlterField: approvals 0004, composer 0021, inbox 0006, media_library 0004, members 0004, notifications 0005.
  - Filtro `role_es` en common_extras.
  - Sin traducir: CSVImportJob.Status (salvo `failed`), choices que solo ve el admin y las consultas de Unsplash.
- Juez: PASA. pytest 1511 passed / 1 preexistente; ruff y mypy limpios.
- Railway: el Custom Start Command de la UI tiene prioridad sobre el CMD del Dockerfile, así que no hay garantía de que corra migrate. Tras el deploy, revisar en Deploy Logs que aparezca «Applying ...»; si no, aplicar las migraciones a mano (ver sesión bean-a9).
- Siguiente: paso 3 (pulido visual), M1/M5 de Meta y T5.

### 2026-10-18 (cont.) — Pulido visual aprobado por mockup + página legal única
- Regla nueva: todo cambio visual pasa primero por un mockup aprobado (`.claude/rules/ui-mockup-first.md`; mockups en `mockups/`, ignorado por git). Agente nuevo `legal-mx`.
- Hecho:
  - Tokens `--st-*`/`--al-*`, Figtree variable, pastillas de estado punteadas.
  - Sidebar, Primeros pasos como pastilla y mini tutorial (`static/js/tour.js`, que guarda en localStorage si ya se vio).
  - Conectar con estado por plataforma; landing y páginas 403/404/500.
  - Página legal única `/legal/` (ES/EN, aside corto como SIMU, `static/js/legal.js`). Responsable y domicilio iguales a los de SIMU. Las URLs viejas redirigen 301 a su ancla. La privacidad declara todos los scopes (se conserva la bandeja social).
- Revisiones: juez PASA (1518 passed, 1 fallo preexistente); design-system-bean y legal-mx con huecos ya corregidos.
- Railway: el Custom Start Command ya incluye migrate y aplicó solo las 6 migraciones de 30d253c. Si hay que migrar a mano: `.venv/Scripts/python.exe C:\Users\ultra\bean-prod-manage.py migrate --noinput` (toca producción: pedir OK al usuario antes).
- Pendientes:
  - **Login con Google:** resuelto (confirmado por el usuario 2026-10-18).
  - **Google OAuth en Testing:** los tokens de YouTube caducan cada 7 días; hay que verificar la marca.
  - **Sombras naranjas** `rgba(249,115,22)` que vienen de BrightBean en composer, media, notificaciones y social_accounts.
  - **TikTok T5 y Meta M5:** pendientes.

### Fase 6 (aprobada por el usuario; arranca después de conectar Meta) — Bandeja completa + Shorts
- Va con mockup primero. Dos apartados: «Publicaciones y comentarios» (FB, IG, YouTube) y «Chats» (Messenger y DMs de IG).
- Bugs actuales que hay que arreglar:
  - **Responder en FB:** a comentarios hace POST `/{comment_id}/messages` (debería ser `/comments`); a DMs de FB/IG pasa el id del mensaje, no el de la conversación (`/me/messages` con `recipient`). Archivos: providers/facebook.py:688 e instagram.py:537.
  - **Webhook de IG:** ignora `field` = comments/mentions (apps/inbox/webhooks.py:161).
  - **Suscripciones:** no hay código que cree `subscribed_apps` en Meta.
  - **Moderación FB/IG:** no implementada (solo YouTube).
- Añadir `instagram_manage_messages` a providers/instagram.py y a la política de privacidad (el usuario ya lo agrega en Meta).
- **YouTube Shorts:** PostType.SHORT existe, pero nada en el composer lo asigna (engine.py:526 `_resolve_post_type`). Falta el selector Short/Video y la distinción en calendario y analíticas.
- **Borrar publicación** desde BEAN (decisión del usuario 2026-10-18): IG con `instagram_manage_contents` (ya agregado en Meta), FB con `pages_manage_posts` y YouTube. TikTok no lo permite por API. Hay que añadir el scope a providers/instagram.py y a la política de privacidad, más un método `delete_post` en los providers y una acción con confirmación en la UI (va en el mockup).
- **Permisos por rol** (después, cuando se configuren los roles): cada función nueva va como permiso propio en el RBAC existente (apps/members: roles, roles personalizados, capacidades por canal como `can_reply`). Lista: responder comentarios, contestar chats, moderar comentarios (ocultar/borrar) y borrar publicaciones. Valores por defecto conservadores: borrar publicaciones solo Propietario/Administrador.
