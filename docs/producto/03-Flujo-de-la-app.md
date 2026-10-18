# Flujo de la app — BEAN by ITHOS

- Fecha: 2026-10-18
- Estado: v1
- Base: `config/urls.py` y los `urls.py` de cada app. Si una ruta cambia, el código manda.

## 1. Mapa de páginas

### Públicas (sin sesión)

| Ruta | Qué es |
|---|---|
| `/` | Landing (anónimo); con sesión es el panel (dashboard) |
| `/review/` | Página para revisores de TikTok/Meta: qué es BEAN y qué integra |
| `/legal/privacy/` | Política de privacidad |
| `/legal/terms/` | Términos de servicio |
| `/legal/support/` | Soporte y contacto |
| `/legal/data-deletion/` | Instrucciones de borrado de datos |
| `/legal/data-deletion/status/<code>/` | Estado de una solicitud de borrado |
| `/legal/data-deletion/callback/` | Callback de Meta (POST `signed_request`); responde `{url, confirmation_code}` |
| `/legal/meta/deauthorize/` | Callback de Meta cuando alguien quita la app (POST `signed_request`) |
| `/tiktok<token>.txt` | Archivo de verificación de URL de TikTok |
| `/accounts/login/`, `/accounts/signup/` (allauth), `/accounts/accept-terms/` | Entrada y alta (registro abierto) |
| `/health/` | Healthcheck de Railway |
| `/.well-known/oauth-*` | Descubrimiento OAuth 2.1 para MCP |

### Callbacks de OAuth (sin sesión de workspace)

`/social-accounts/callback/<platform>/` — `facebook`, `instagram`, `youtube`; TikTok usa el alias opaco `social1` (`apps/social_accounts/oauth_aliases.py`) porque TikTok rechaza redirect URIs que contengan «tiktok».

### Privadas (con sesión; `RBACMiddleware` resuelve org y workspace)

| Ruta | Qué es |
|---|---|
| `/` | Panel |
| `/accounts/settings/`, `/accounts/logout/` | Ajustes personales, salir |
| `/organizations/settings/` | Ajustes de la organización; aquí está **Procesar pendientes** |
| `/organizations/workspaces/`, `/organizations/calendar/` | Workspaces y calendario global |
| `/organizations/api-keys/` | Claves de la API de agentes |
| `/workspaces/`, `/workspaces/create/`, `/workspaces/<id>/settings/` (+ `approvals/`) | Gestión de workspaces |
| `/members/` (+ `invite/`, rol, quitar) | Equipo e invitaciones |
| `/settings/` | Ajustes en cascada (workspace → org → defaults) |
| `/social-accounts/<ws>/` | Cuentas conectadas |
| `/social-accounts/<ws>/connect/` | Conectar una plataforma |
| `/social-accounts/select-account/` | Elegir Página/cuenta tras el OAuth (Facebook multi-página) |
| `/social-accounts/<ws>/<acc>/reconnect/`, `/disconnect/`, `/delete/` | Reconectar, desconectar, borrar cuenta |
| `/workspace/<ws>/create/` | Elegir qué crear |
| `/workspace/<ws>/compose/` (+ `<post>/`) | Composer |
| `/workspace/<ws>/drafts/`, `categories/`, `templates/`, `tags/`, `ideas/board/`, `import/csv/` | Borradores, categorías, plantillas, etiquetas, ideas, importación |
| `/workspace/<ws>/compose/tiktok-creator-info/<acc>/` | JSON con nickname y opciones de privacidad de TikTok (se consulta al componer) |
| `/workspace/<ws>/calendar/` | Calendario; `publish/queue/`, `publish/drafts/`, `publish/approvals/`, `publish/sent/` son las pestañas de Publish |
| `/workspace/<ws>/calendar/posting-slots/`, `queues/` | Huecos de publicación y colas |
| `/workspace/<ws>/approvals/<post>/approve|request-changes|reject|resume/`, `approvals/bulk/` | Acciones de aprobación |
| `/workspace/<ws>/inbox/` | Bandeja unificada |
| `/workspace/<ws>/analytics/` (+ `<account>/`, `post/<post>/`) | Analytics |
| `/workspace/<ws>/media/` | Biblioteca de medios |
| `/workspace/<ws>/settings/clients/` | Administración del portal de clientes |
| `/notifications/` | Notificaciones |
| `/tasks/process/` (POST) | Procesar pendientes |
| `/cron/run` (POST, Bearer) | Lo mismo, para un cron externo |
| `/portal/…` | Portal de clientes (acceso por enlace con token) |
| `/api/v1/…`, `/api/v1/mcp`, `/oauth/…` | API de agentes, MCP, OAuth 2.1 |

## 2. Recorridos

**Conectar una cuenta.** Cuentas → Connect → elegir plataforma → BEAN firma un `state` y redirige a la plataforma → el usuario concede permisos → vuelve a `/social-accounts/callback/<platform>/` → BEAN cambia el código por tokens (cifrados) → si es Facebook/Instagram con varias Páginas, `select-account` → la cuenta aparece en la lista.

**Componer.** Compose → elegir cuentas (cada una puede tener su variante) → texto y medios (biblioteca o subida) → ajustes por plataforma (YouTube: visibilidad; TikTok: nickname, privacidad sin valor por defecto, comentar/dúo/stitch, divulgación comercial, declaración) → vista previa → guardar (autoguardado como borrador).

**Aprobar.** Si el workspace exige aprobación, el post pasa a `pending_review` (o `pending_client`); el revisor aprueba, pide cambios o rechaza (con comentario); lo pedido vuelve al autor como `changes_requested`. En uso personal se puede saltar la aprobación.

**Programar.** Elegir fecha/hora, o añadir a una cola (`Queue`) que toma el siguiente `PostingSlot` libre. Los `PlatformPost` quedan `scheduled`.

**Procesar pendientes y publicar.** Nada sale solo. El usuario pulsa «Procesar pendientes» (Ajustes de la organización) o un cron llama `/cron/run`: se corre el ciclo del publicador (los vencidos pasan a `publishing`, luego `published` o `failed`, con reintentos 60 s / 5 min / 30 min) y se drenan las demás tareas. «Publicar ahora» en el composer hace lo mismo para ese post.

**Ver analytics.** Analytics → general, por cuenta o por publicación. Se sincroniza con las tareas recurrentes (también se drenan al procesar).

**Desconectar.** Cuentas → Disconnect (revoca el token si el proveedor lo permite y deja el historial) o Delete (borra la cuenta; la pantalla avisa cuántos posts quedan huérfanos). Para borrar todos los datos: `/legal/data-deletion/`.

## 3. Estados de un post

La fuente de verdad es **`PlatformPost`** (una fila por cuenta destino). `Post.status` es **derivado** por `derive_post_status()` (`apps/composer/status.py`); nunca se escribe en `Post`.

Estados de `PlatformPost`: `draft`, `pending_review`, `pending_client`, `approved`, `changes_requested`, `rejected`, `on_hold`, `scheduled`, `publishing`, `published`, `failed`.

Reglas del derivado: sin hijos → `draft`; todos iguales → ese; todos terminales → `published` / `failed` / `partially_published` si hay mezcla; algún `failed` con hijos aún en curso → `publishing`; en otro caso gana el «más bajo» (más pendiente de acción) del orden `draft < changes_requested < rejected < on_hold < pending_review < pending_client < approved < scheduled < publishing < partially_published < published`. `on_hold` con algún `published` se reporta `partially_published`.

```
draft → pending_review → approved → scheduled → publishing → published
          ↘ changes_requested / rejected        ↘ failed (reintento)
```
Los `PlatformPost` en `published`/`publishing` están protegidos contra borrado accidental.
