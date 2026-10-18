# PRD — BEAN by ITHOS

- Fecha: 2026-10-18
- Estado: v1
- Base: `CLAUDE.md`, `HANDOFF.md`, `vault/` y el código (apps/ y providers/).

## 1. Visión

BEAN (BEAN by ITHOS) es un lugar tranquilo y propio donde ITHOS y sus marcas (SIMU y las que vengan) preparan, aprueban, programan y publican sus contenidos en redes, y leen cómo les fue, sin depender de una herramienta de pago de terceros ni de un servidor encendido las 24 horas.

Es un fork personal y no comercial de BrightBean Studio (AGPL-3.0). No se vende ni se ofrece como SaaS público. El repo es público (`TenDanOne/bean`) y el aviso de AGPL se conserva.

## 2. Problema

- Publicar a mano en YouTube, Facebook, Instagram y TikTok es repetitivo y se olvida.
- Las herramientas comerciales cobran por red/marca y guardan los datos en otro sitio.
- Un worker 24/7 cuesta dinero y no hace falta para el volumen actual.

## 3. Audiencia

- **Principal:** el dueño de ITHOS, que opera las redes de ITHOS y de SIMU (un solo usuario hoy).
- **Secundaria:** colaboradores o clientes de las marcas, con los roles que BrightBean ya trae (owner/admin/member en la organización; owner, manager, editor, contributor, client, viewer en el workspace). Los roles se conservan intactos aunque hoy se use una sola persona.
- Estructura: Organización (ITHOS) → Workspace (una marca, p. ej. SIMU) → cuentas sociales conectadas.

## 4. Alcance

**Plataformas activas:** YouTube; Facebook (Páginas); Instagram profesional (cuenta Business/Creator ligada a una Página, vía Graph API de Facebook); TikTok (Login Kit + Content Posting API, Direct Post). Las demás plataformas de BrightBean se ocultan con `DEFAULT_ENABLED_PLATFORMS` (`apps/social_accounts/platforms.py`, override por `BBS_ENABLED_PLATFORMS`), nunca se borran.

**Funciones reales** (todas heredadas de BrightBean y verificadas en `urls.py`):

| Función | Dónde |
|---|---|
| Composer: una publicación, una variante por cuenta (`platform_specific_*`), medios, primer comentario, plantillas, categorías, etiquetas, ideas (tablero), importación CSV, clonar | `apps/composer` |
| Calendario y pestaña Publish (cola, borradores, aprobaciones, enviados), mover fechas, eventos | `apps/calendar` |
| Cola por huecos: `PostingSlot` (horarios semanales por cuenta) y `Queue`/`QueueEntry` (cola con orden que asigna el siguiente hueco libre) | `apps/calendar/models.py` |
| Aprobaciones: aprobar, pedir cambios, rechazar, reanudar, comentarios, versiones, acciones en lote; portal para clientes | `apps/approvals`, `apps/client_portal` |
| Publicador: sondea cada ~15 s los `PlatformPost` vencidos, reintentos 60 s / 5 min / 30 min, `PublishLog` | `apps/publisher` |
| Inbox: comentarios y mensajes unificados, respuestas, notas, asignación, respuestas guardadas, SLA | `apps/inbox` |
| Analytics por cuenta y por publicación | `apps/analytics` |
| API de agentes `/api/v1/` (claves `bb_studio_…`) y servidor MCP `POST /api/v1/mcp` con OAuth 2.1 | `apps/api`, `apps/mcp`, `apps/oauth_server` |
| Biblioteca de medios (almacenamiento en Cloudflare R2) | `apps/media_library` |

**Modo manual sin worker.** No hay proceso 24/7. Un post programado sale cuando el usuario pulsa «Procesar pendientes» (ajustes de la organización, `POST /tasks/process/`, drena la cola de django-background-tasks) o cuando algo externo llama `POST /cron/run` con `Authorization: Bearer <CRON_SECRET>`. Ese endpoint está dormido: 503 si no hay `CRON_SECRET`, 401 con bearer incorrecto. «Publicar ahora» corre el ciclo de publicación y drena al instante.

**Despliegue:** Railway (un servicio web), Supabase (Postgres), Cloudflare R2, `bean.ithos.mx`, Sentry opcional, MailerSend por SMTP.

## 5. Principios

1. No romper lo existente (pytest + ruff + mypy contra la línea base).
2. Tokens de redes cifrados (AES-256-GCM); nunca en logs ni en git.
3. Uso personal: lo mínimo que funcione; sin features nuevas por adelantado.
4. Marca ITHOS (paleta A «Oliva y petróleo», Figtree), sin logos de BrightBean.

## 6. Riesgos

| Riesgo | Efecto | Mitigación |
|---|---|---|
| Un post programado no sale si nadie procesa | Publicación tardía | Botón «Procesar pendientes»; opcional cron externo contra `/cron/run` |
| Meta y TikTok no aprueban la app | Solo cuentas con rol en la app (Meta) / solo privado (TikTok sin auditoría) | `docs/APP-REVIEW.md`: textos, guion y brechas |
| Pedir más permisos de los que se usan | Rechazo en App Review | Recortar scopes (ver brechas) |
| Cambio de `SECRET_KEY` o `ENCRYPTION_KEY_SALT` | Tokens cifrados quedan huérfanos | No cambiarlos; respaldo fuera del repo |
| Registro abierto (decisión del usuario) | Cuentas ajenas | Revisar; cerrar el registro si aparece abuso |
| Límites de plataforma (IG 100 posts/24 h; TikTok 5 usuarios/24 h sin auditoría) | Rechazos al publicar | Mensajes de error del proveedor |
| AGPL | Obligación de ofrecer el código | Repo público, `LICENSE` intacto, aviso en el pie |

## 7. Fuera de alcance

- Venta, planes de pago, multiusuario comercial.
- Plataformas ocultas (LinkedIn, X, Threads, Pinterest, Bluesky, Mastodon, Google Business, etc.): siguen en el código pero apagadas.
- Worker 24/7 y Redis/Celery.
- Marcar la app como producto de terceros; logos de BrightBean.
- Instagram Login directo (`providers/instagram_login.py`): oculto, no usado.
