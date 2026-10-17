# BBS — arquitectura actual, alcance decidido y plan de evolución

Plataforma propia de gestión de redes sociales, tomando **AgoraPulse como referencia** de a qué debe parecerse, y **BrightBean Studio como base de código**.

Regla que gobierna todo el plan: **añadir sin romper**. Cada fase entra con test que fija la conducta anterior antes de tocarla.

---

## Parte I — Qué hay hoy (medido)

| Dimensión | Cantidad |
|---|---|
| Apps Django | 22 |
| Modelos | 68 |
| Rutas URL | ~205 (composer 57, calendar 22, intelligence 18, media 17, inbox 13) |
| Tareas de fondo | 11 |
| Plataformas | 13 |
| Puntos donde se aplica permiso | 95 (67 `@require_permission` + 28 decoradores de rol) |
| Consultas que tocan `SocialAccount` | 49, en 13 apps |
| Tokens de diseño CSS | ~85 |

Los comandos que generan estas cifras están en el anexo, para poder auditarlas y que no envejezcan en silencio.

**Interfaz:** todo cuelga de [templates/base.html](../templates/base.html) (1392 líneas, contiene el sidebar entero); solo hay 2 layouts más. Navegación actual: Publish, Create Idea, Social Inbox, Notifications, Analytics, Intelligence, Channels.

**Sano y no se toca:** capa de proveedores (13 ficheros tras `PROVIDER_REGISTRY`, sin ramas por plataforma en los llamantes), motor de publicación, cascada de ajustes, cifrado de tokens, y la tenencia `Organization → Workspace → SocialAccount`.

### Mapa funcional por área

| App | Funciones | Qué hace |
|---|---|---|
| composer | 51 | Componer, autoguardado, preview en vivo, versiones, media, miniaturas, Unsplash, ideas kanban, plantillas, CSV |
| calendar | 22 | Vistas mes/semana/día/lista, reprogramar arrastrando, franjas, colas, eventos |
| media_library | 26 | Biblioteca, subida, carpetas, etiquetas, versiones, biblioteca compartida |
| inbox | 13 | Feed con filtros, hilo, responder, notas, asignar, estado, sentimiento |
| onboarding | 11 | Enlaces para que un tercero conecte sus cuentas |
| approvals | 9 | Aprobar, pedir cambios, rechazar, comentarios, diff de versiones |
| social_accounts | 9 | Conectar, callback OAuth, reconectar, desconectar |
| members | 8 | Invitar, roles, quitar, asignar workspaces |
| client_portal | 7 | Enlace mágico, panel y cola de aprobación para cliente externo |
| analytics | 3 | Índice, detalle por cuenta, detalle por post |

### Huecos medidos

**Permisos desiguales.** inbox 13/13 y approvals 9/9 están bien; media_library 19/26, composer 21/51, calendar 8/22, y **analytics 0/3**: solo comprueba pertenencia al workspace ([analytics/views.py:37](../apps/analytics/views.py#L37)) y nunca la clave `view_analytics` — el rol `contributor`, el único al que se le niega, entra igual.

**White-label roto por construcción.** Existen ~85 tokens y la promesa de color de marca por organización, pero hay **1.868 clases de color a pelo** (`text-stone-700`, `bg-white`) contra **1.398 usos de token**: el 57 % de las decisiones de color no responde al color de marca. `compose.html` concentra 299.

**Construido a medias.** `InboxMessage.related_post` está declarado y relleno en 0 de 45 filas. `inbox.sync_interval_minutes` es un ajuste que no lee nadie. No hay auditoría de acciones humanas. No existe relación usuario↔canal.

**Riesgo activo.** Desconectar un canal borra sus posts y, por CASCADE, sus mensajes ([social_accounts/views.py:793](../apps/social_accounts/views.py#L793)), sin confirmación.

---

## Parte II — Alcance decidido

### Se elimina

| Qué | Tamaño | Motivo |
|---|---|---|
| **Intelligence** | 4 modelos, 18 rutas, 42 plantillas, Stripe | BBS es interno; no se vende acceso. Mayor reducción de superficie del repo. |
| **Enlaces de conexión (onboarding)** | 3 modelos, 11 rutas | El owner conecta los canales desde dentro. Elimina un flujo OAuth paralelo al normal. |
| **Feeds de contenido** | parte de composer | No encaja con un flujo propio. |
| **Biblioteca compartida de organización** | 3 rutas de media | La media se separa por workspace: la de una marca no se mezcla con la de otra. |

### Se conserva apagado

Client Portal (1 modelo, 11 rutas, 14 plantillas) sin entrada en navegación — la revisión externa se resuelve ahora con el rol Reviewer, sin enlaces mágicos. Roles `client` y `contributor` ocultos del selector pero vivos en base de datos.

**Colas, franjas y recurrencia se ocultan de la interfaz hasta verificarlas.** Son 19 rutas y 4 modelos que hoy aparecen en el calendario y que nadie ha probado nunca. Dado que ya aparecieron seis casos de «la pantalla está, el backend está a medias», dejarlas visibles sin garantía es exponer al equipo a un fallo. Se encienden el día que se prueben.

### Se conserva activo

API REST + MCP + servidor OAuth + claves (4 apps). Aprobaciones con comentarios, comparación de versiones y recordatorios automáticos. Ideas/Kanban como función secundaria dentro del composer. Composer con plantillas de post e importación CSV. Media con carpetas y etiquetas. Inbox con respuestas guardadas, SLA, notas internas y sentimiento. Notificaciones in-app y correo.

**Plataformas:** las 13 se quedan en código; visibles para conectar solo **YouTube, TikTok, Instagram y Facebook**. El resto se oculta con un interruptor, reactivable sin tocar código.

---

## Parte III — Tenencia

Una organización = una empresa. Dentro, un workspace por marca o cliente, con sus canales y su equipo. Un miembro puede pertenecer a **varios workspaces con rol distinto en cada uno** — gestionar el YouTube del cliente A y contestar el Instagram de la marca B. **Ya soportado, sin trabajo de arquitectura.**

Otra empresa = otro owner y otra organización, aisladas entre sí; también funciona hoy. Limitación conocida que no se aborda: una misma persona no puede pertenecer a dos organizaciones (`RBACMiddleware` resuelve con un `.first()`).

---

## Parte IV — Roles y permisos por canal (el núcleo)

### Jerarquía

**Owner → Manager → Editor → Reviewer → Lector.** `client` y `contributor` se ocultan del selector.

| Capacidad | Owner | Manager | Editor | Reviewer | Lector |
|---|---|---|---|---|---|
| Renombrar workspace | Sí | No | No | No | No |
| Conectar / archivar canales | Sí | No | No | No | No |
| Asignar canales a miembros | Sí | No | No | No | No |
| Añadir miembros | Sí | Sí (por debajo suyo) | No | No | No |
| Crear workspaces | Sí | No | No | No | No |
| Aprobar | Sí | Sí | No | Sí | No |
| Editar posts ajenos | Sí | Sí | No | No | No |

Todo lo demás pasa por la asignación de canal.

### Asignación de canal

`ChannelAssignment(workspace_membership, social_account, …)` con `unique_together`, y **seis casillas independientes**, no una escalera acumulativa:

| Casilla | Qué habilita |
|---|---|
| `ver` | Implícita al asignar: ve el canal y sus comentarios. |
| `ver_metricas` | Analytics de ese canal. Separada a propósito: se puede contestar sin ver rendimiento, o al revés. |
| `responder` | Contestar comentarios y mensajes. |
| `publicar` | Crear y programar en ese canal. |
| `moderar` | Ocultar o marcar como spam. |
| `borrar` | Eliminar comentarios. La más delicada. |

**Aprobar no es una casilla: viene del rol.** Owner y manager aprueban por defecto; el rol Reviewer se concede a quien deba revisar sin crear nada. Como la visibilidad va por asignación, un Reviewer solo aprueba los canales que se le hayan asignado — que es lo que resuelve el caso del dueño de una cuenta personal sin necesidad de un mecanismo aparte.

**Regla de visibilidad: solo el owner ve todo sin asignación.** Manager, editor, reviewer y lector necesitan asignación explícita; sin ella no ven nada, con un aviso claro de a quién pedirla.

**Reparte solo el owner.** El manager coordina el trabajo pero no concede accesos, haya uno o varios managers. Es más control y trazabilidad a cambio de que el owner sea el cuello de botella si el equipo crece — compensación aceptada a conciencia.

**Aplicación y su coste real.** Hay 49 consultas que tocan `SocialAccount` en 13 apps. Filtrarlas todas de golpe es la forma de romper cosas. El filtro se aplica **solo en comentarios, mensajes, composer y analytics**; el resto queda documentado como deuda conocida en lugar de fingir cobertura total.

La capa es **ortogonal** a las 14 claves existentes: un editor necesita `reply_from_inbox` (rol) **y** `responder` sobre ese canal. Ninguna de las 95 comprobaciones actuales cambia de significado. `edit_others_posts` («solo edito lo mío») **ya funciona** — enforced en 4 sitios; no se toca.

---

## Parte V — Bandeja partida en dos

`message_type` ya distingue `dm`, `comment`, `mention` y `review`, así que el eje de separación existe en los datos.

- **Comentarios** — agrupados por publicación, en carpetas. Se apoya en `extra["video_id"]`, que ya agrupa limpio (7 comentarios en un vídeo, 5 en otro, 3 en otro), y en rellenar `related_post`, hoy vacío en las 45 filas.
- **Mensajes** — DMs, lista plana.

Una sola entrada «Bandeja» en el lateral, con dos pestañas dentro.

**Sentimiento manual.** El modelo ya trae `sentiment_source` con valores `auto` y `manual`, y existe la vista `change_sentiment`. Se cambia el valor por defecto a manual: lo etiqueta quien responde. El análisis automático actual es por palabras, no IA, y en español acertaría poco.

---

## Parte VI — Registro de actividad

`ActivityLog(workspace, actor, verb, target_type, target_id, metadata, created_at)`.

**Escritura por llamada explícita, no por señales.** Un catch-all sobre `post_save` registra ruido, no intención: no distingue «el usuario programó un post» de «un job tocó una fila». Se instrumentan las mutaciones con significado de negocio: respuesta enviada, post creado / programado / publicado, miembro añadido o cambiado de rol, canal conectado / archivado, asignación de canal modificada, aprobación concedida o denegada.

**Visibilidad:** owner y manager ven la actividad de todo su workspace; cada miembro ve la suya.

**Superficies:** últimas 10 acciones del equipo, y exportación por día, semana o mes y por miembro.

---

## Parte VII — Analytics

Se conserva lo existente (por canal y por publicación) y se añade **por miembro**, que es diseño nuevo:

- **Rendimiento de lo publicado** — cruzar métricas con `Post.author`, que ya existe.
- **Volumen de trabajo** — posts creados y comentarios respondidos por canal; sale del registro de actividad.
- **Tiempo de respuesta** — cuánto tarda cada persona en contestar; se apoya en el SLA que se conserva.

---

## Parte VIII — Desconectar un canal

Dos acciones distintas donde hoy hay una sola, destructiva:

- **Archivar** (por defecto): el canal deja de sincronizar, pero publicaciones, comentarios y métricas se conservan como archivo consultable. Exige marcar el canal inactivo en vez de borrarlo.
- **Eliminar definitivamente**: con confirmación que dice exactamente qué se pierde («42 publicaciones y 380 comentarios») y pide escribir el nombre del canal.

---

## Parte IX — Navegación e interfaz

**Publish · Bandeja (Comentarios / Mensajes) · Analytics · Actividad · Canales del workspace**

La lista de canales del workspace se mantiene visible para ver de un vistazo qué hay en cada marca. Ideas sale del lateral y vive dentro del composer. Intelligence desaparece.

**Criterio de permisos en la interfaz** (ya aplicado parcialmente): **deshabilitar** lo que el rol no permite —visible, inerte, con el motivo en el tooltip— y **ocultar** solo lo que filtra secretos o dinero. Ya existen las páginas 403/404/500 con causa, la variante compacta para HTMX y el filtro de plantilla `can`.

---

## Parte X — Orden de ejecución

| Fase | Qué | Por qué ahí |
|---|---|---|
| **0** | Archivar / eliminar canal con confirmación | Única ruta que destruye trabajo sin preguntar. |
| **1** | Podar: Intelligence, onboarding, feeds, biblioteca compartida. Ocultar roles muertos, plataformas no usadas y colas/franjas | Reduce superficie antes de construir encima. |
| **2** | Completar enforcement donde falta, empezando por `view_analytics` | Barato y cierra un hueco de permisos real. |
| **3** | Rol Reviewer + `ChannelAssignment` + resolución + filtrado + UI de asignación | Pieza de la que dependen las demás. |
| **4** | Bandeja partida: comentarios por publicación y mensajes | Necesita la 3 para filtrar por canal asignado. |
| **5** | `ActivityLog` + últimas 10 + exportación | Necesita la 3 para registrar «quién sobre qué canal». |
| **6** | Analytics por miembro | Necesita la 5 para volumen y tiempo de respuesta. |
| **7** | Repaso sistemático de calendario y media | Buscando el patrón que ya apareció seis veces. |
| **8** | Migración de color a tokens | Mecánico y toca muchos ficheros: mejor sin cambios funcionales encima. |

---

## Anexo A — Bugs encontrados y corregidos

Seis casos del mismo patrón —interfaz presente, backend incompleto—, ninguno visible hasta usarlo. Son la evidencia que justifica la fase 7.

| Bug | Causa raíz |
|---|---|
| Las invitaciones nunca se enviaban | `development.py` forzaba el backend de consola, ignorando `EMAIL_BACKEND_TYPE` |
| Cualquier filtro vaciaba el inbox | `getlist()` devolvía `[""]`, truthy, y filtraba por cadena vacía |
| Token de YouTube caducado 5 de cada 6 h | Refresco cada 6 h para tokens que duran 1 h; el inbox no refrescaba antes de usar |
| Respuestas que fingían haberse enviado | El fallo del proveedor se tragaba y se guardaba la respuesta igual |
| La miniatura no llegaba al preview | El preview solo leía los adjuntos, nunca `yt_thumbnail_asset_id_*` |
| El inbox se contaminaba con su propia voz | No se distinguía el comentario propio del ajeno, ni se enlazaba respuesta con su padre |

Además, un foot-gun recurrente del repo: los comentarios `{# … #}` de Django **solo funcionan en una línea**; escritos en varias se renderizan como texto visible. Ya había ocurrido antes en el sidebar.

## Anexo B — Comandos que generan las cifras

```bash
# Modelos por app
for d in apps/*/; do grep -c "^class " "$d/models.py" 2>/dev/null; done

# Puntos donde se aplica permiso
grep -rn "@require_permission" --include=*.py apps/ | grep -v test | wc -l
grep -rn "@require_workspace_role\|@require_org_role" --include=*.py apps/ | grep -v test | wc -l

# Consultas que tocan SocialAccount
grep -rn "social_account=\|social_account__in\|SocialAccount.objects" --include=*.py apps/ | grep -v test | grep -v migrations | wc -l

# Adopción de tokens de diseño frente a color a pelo
grep -ro "var(--[a-z0-9-]*)" templates/ | wc -l
grep -roE "(text|bg|border)-(stone|gray|slate|zinc)-[0-9]{2,3}" templates/ | wc -l
```
