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

### 2026-10-19 — Pulido visual 2 (mockup aprobado `mockup-pulido-2`, borrado al implementar)
- Hecho (sesión bean-fb; parte va dentro del commit 228bf0c de bean-db por archivos compartidos):
  - Sidebar: logo BEAN by ITHOS siempre arriba, selector de workspace debajo, isotipo recortado en colapsado y cabecera móvil con logo (`templates/base.html`).
  - Publicar: chips-filtro de estado arriba con conteo y sin palomita (`templates/calendar/partials/status_chips.html`, `STATUS_CHIPS` en `apps/calendar/views.py`; «En pausa» va dentro de «En revisión»). El mes ya no tiene scroll interno. Nuevo look de `--st-publishing-*` (fondo claro y punto pulsante); `.sidebar-badge-alert` usa `--st-scheduled-fg`.
  - Bandeja: tarjeta de la publicación arriba del hilo (`_post_card` en `apps/inbox/views.py`), respuestas en hilo, filtros en una fila. «Tú · BEAN» solo si la respuesta es del usuario actual. Ya no truena con autor nulo (`_author_name`).
  - Legal: una página por documento. ES `/privacidad/ /terminos/ /borrado-de-datos/ /contacto/` y EN `/en/privacy/ …` (`public_legal_doc`, `templates/public/legal_base.html` + `templates/public/legal/`). `/legal/*` redirige 301; callbacks de Meta intactos. Texto legal sin cambios (comparado palabra por palabra). `docs/APP-REVIEW.md` y `GUIA-USO.md` actualizados.
  - Conectar: botones alineados al fondo. Plural «notificación/notificaciones». Migraciones 0004 de organizations/workspaces renombran «My Organization»/«My Workspace».
- Juez: PASA, sin regresiones. Revisión con design-system-bean: PASA.
- Pendientes:
  - `.claude/agents/legal-mx.md` aún menciona `/legal/`.
  - En la sidebar, `href="/"` en vez de `{% url %}`.
  - Revisar Publicar en navegador (escritorio y móvil).
  - Actualizar las URLs en las consolas de Meta, Google y TikTok a `/en/...` (las viejas siguen funcionando por 301).

## RETOMAR AQUÍ — 2026-10-19 (Fase 6: lotes A1 y A2 en main)
- En main: 228bf0c (lotes A1+A2) + 6cf3a12 (sesión bean-fb: pulido visual 2, legal partido, chips de estado, migraciones 0004 de nombres por defecto). Juez: PASA; pytest 1591 passed + 1 preexistente.
- Hecho:
  - aprobación obligatoria real;
  - overrides persistentes;
  - recuperación de PUBLISHING atascado;
  - reintentos con next_retry_at;
  - validación de medios;
  - chunks en TikTok y resumable en YouTube (solo con mocks);
  - CSV robusto;
  - arrastre con hora;
  - tokens frescos y long-lived de Meta (verificar «Expires: Never» en el debugger);
  - notificaciones reales y digest;
  - archivadas;
  - login (contraseña sin la actual si entras con Google; borrar cuenta escribiendo el correo).
- Pendientes menores de A2-8:
  - pestaña Preferencias;
  - placeholder de /settings/;
  - menú de Ajustes por permisos;
  - «Repetir recorrido» en el menú;
  - errores XHR de la biblioteca de medios;
  - borrar un recurso en uso.
- Pendientes del juez: DNS rebinding del CSV (usar resolve_public_ip), archivos R2 huérfanos y CSV sin process_media_asset, YouTube 308 sin leer Range, keep_time cuando pp.scheduled_at es nulo.
- Cron: el usuario está configurando cron-job.org → POST /cron/run cada 5 min (pendiente confirmar el Test run 200).
- Siguiente: Lote B (bandeja nueva + Shorts + tipos de post + borrar publicación), con mockup primero.

## RETOMAR AQUÍ — 2026-10-19 (Fase 6 lote B completo)
- En main:
  - c705c6c: motor de la bandeja, tipos de post y borrar publicación.
  - Este commit: interfaz nueva de la bandeja según mockups/mockup-bandeja.html (No contestados / Comentarios / Chats, estados de una palabra a la derecha, ayuda «?», cuentas en 2 columnas).
- Decisiones del usuario:
  - Se conservan búsqueda, respuestas guardadas, notas internas y asignar.
  - Lote, sentimiento y SLA salen de la UI (los endpoints siguen vivos).
  - «Ocultar» en YouTube se restauró (regresión detectada por el juez).
- Juez PASA; pytest 1699 passed + 1 preexistente.
- El usuario debe reconectar Facebook e Instagram (scopes nuevos + user id de Meta + token long-lived).
- Pendientes:
  - **App:** pestaña Preferencias, /settings/, menú de Ajustes por permisos, «Repetir recorrido», errores XHR de la biblioteca de medios.
  - **CSV y subidas:** DNS rebinding del CSV, archivos huérfanos en R2, Range del 308 en YouTube.
  - **Webhooks y bandeja:** suscripción de webhooks de IG sin confirmar; message_detail huérfano; el badge cuenta mensajes y la pestaña cuenta conversaciones.
  - **Cron:** cron-job.org sin confirmar.

### Pendientes del usuario (2026-10-19, los hará después)
- Reconectar Facebook e Instagram en BEAN (scopes nuevos, user id de Meta, token long-lived) y probar la bandeja con comentarios y DMs reales. Verificar «Expires: Never» en el debugger de tokens.
- Crear la app en TikTok for Developers y pedir la auditoría (sin ella, los posts salen privados).
- Verificación de Google OAuth (Producción + permisos sensibles de YouTube) para que los tokens no caduquen cada 7 días.
- Cron: cron-job.org → POST /cron/run cada 5 min. HECHO: Test run 200 OK (5,7 s).

## RETOMAR AQUÍ — 2026-10-20 (Fase 6 lote C: publicar)
- En main:
  - 8a6b96d (C1, arreglos de publicar):
    - miniatura de YouTube con error visible, ≤2 MB y 1280×720, acotada al workspace;
    - vista previa al día;
    - miniatura elegida en las listas;
    - tags internos que no se filtran;
    - videos de IG por urlparse;
    - plantillas completas y bloqueo de {{…}};
    - subida directa a R2 (falta poner el CORS: docs/r2-cors-subida-directa.md);
    - duración Short/Reel.
  - Este commit (C2, composer nuevo según mockups/mockup-publicar.html):
    - 3 pasos (formato → contenido → cuándo), con pestañas por red para la vista previa y los campos;
    - YouTube: categoría, playlist, capítulos, «Hecho para niños» obligatorio en UI, cola y aprobación, IA;
    - IG: portada del Reel, colaboradores, share_to_feed;
    - FB: enlace y Reels;
    - revisor de calidad (apps/composer/quality.py) con frases prohibidas por workspace;
    - 6 plantillas por formato (ids 162-167);
    - /ayuda/ con glosario;
    - un solo «Nuevo» y subida con orden.
- Juez PASA; pytest 1799 passed y 0 fallos (también se arregló el preexistente test_preview).
- Pendientes:
  - **Probar contra APIs reales:** FB Reels (video_reels), playlistItems, subida directa a R2 (con CORS), selector de fotogramas y playlists con token real.
  - **API/MCP:** crean posts de YouTube con made_for_kids=False sin preguntar; documentarlo o agregar un campo.
  - **Menores:** pestaña Preferencias, /settings/, menú de Ajustes por permisos.

### Siguiente lote (después del blindaje de publicación y el modal «Publicando»), decidido 2026-10-20
- **Música** (`bean.json` → `musica`: tiktok/instagram/youtube/facebook = en_app | libre_en_mp4, sugerencia, golpe_seg, nota):
  - TikTok: OPCIÓN por publicación en la pestaña TikTok del composer, «Publicar directo» o «Enviar como borrador a TikTok (para poner la música en la app)». Con en_app viene preseleccionado «borrador», pero el usuario decide. Borrador = inbox con scope video.upload; mostrarlo en el demo de TikTok.
  - IG/YouTube/FB en_app → «publicar a mano desde la app», con la sugerencia y el golpe.
  - libre_en_mp4 sin mp4 → se trata como en_app.
- **Portadas:**
  - portada_ruta → cover de IG y miniatura de YouTube largo;
  - portada_seg → cover de TikTok y thumb_offset de IG;
  - FB Reels: miniatura si la API lo admite.
- Los zips nuevos de posts usan *_con_portada.mp4 (la portada es el último cuadro).

## RETOMAR AQUÍ — 2026-10-20 (publicación blindada, importador, TikTok borrador)
- En main desde el último RETOMAR:
  - **Importar publicación (.zip):** un paquete de la sesión de posts por publicación llena el composer.
  - **Modales:** dialog y popover centrados.
  - **Fallidos:** «Eliminar fallidos» y papelera dentro del chip.
  - **Calendario:** «+N más» expandible.
- **Bugs críticos corregidos:**
  - TikTok pending volvía a «Programado» por guardar un objeto viejo, y podía republicarse (e20bdcd, con migración composer 0022 que reparó las filas);
  - guardas contra doble publicación: tareas posteriores aisladas, _already_published, may_have_published en el paso final y publish_id conservado.
- **Modal «Publicando»:** avance por red, botón bloqueado.
- **Estado agregado:** published + scheduled = partially_published (editable); «Publicando» en petróleo con pulso.
- **TikTok:**
  - «Publicar directo» o «Enviar como borrador» (inbox, scope video.upload) por publicación;
  - SEND_TO_USER_INBOX es final → «Borrador en TikTok»;
  - mientras la app no esté auditada, la CUENTA debe ser privada + «Solo yo».
- **«A mano»:** IG/YT/FB con música de la app; manual_publish, sin API y sin validaciones de YouTube.
- **Importador:** lee musica y portadas.
- pytest 1893 passed / 1 skipped (test de zips reales: requiere BEAN_TEST_PACKAGES_DIR).
- **Pendiente del usuario:** grabar el demo de TikTok con la cuenta privada (escena extra de borrador; ver docs/APP-REVIEW.md) y enviar a revisión; reconectar FB/IG; CORS de R2; verificar el dominio ithos.mx en TikTok (para fotos y PULL_FROM_URL); S3_CUSTOM_DOMAIN para media.
