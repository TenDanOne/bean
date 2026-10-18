# App Review — BEAN by ITHOS (Meta y TikTok)

- Fecha: 2026-10-18
- Estado: v1 (borrador listo para completar; ver §5 «Brechas»)
- Fuentes del código: `providers/facebook.py`, `providers/instagram.py`, `providers/tiktok.py`, `apps/social_accounts/views.py`, `apps/social_accounts/oauth_aliases.py`, `templates/composer/compose.html`.
- Fuentes oficiales consultadas el 2026-10-18 (reverificar antes de enviar; Meta y TikTok cambian sus reglas):
  - Meta, permisos: https://developers.facebook.com/docs/permissions
  - Meta, App Review: https://developers.facebook.com/docs/resp-plat-initiatives/individual-processes/app-review
  - Meta, borrado de datos: https://developers.facebook.com/docs/development/create-an-app/app-dashboard/data-deletion-callback
  - Meta, publicación en Instagram: https://developers.facebook.com/docs/instagram-platform/content-publishing
  - TikTok, guías de UX (Direct Post): https://developers.tiktok.com/doc/content-sharing-guidelines
  - TikTok, crear la app y revisión: https://developers.tiktok.com/doc/getting-started-create-an-app

Contexto para el revisor: BEAN es una aplicación interna de ITHOS para publicar en sus propias cuentas (y las de sus marcas, como SIMU). No se vende ni es SaaS público. La página `https://bean.ithos.mx/review/` lo explica.

## 0. URLs comunes

| Campo | Valor |
|---|---|
| App / sitio web | `https://bean.ithos.mx/` |
| Política de privacidad | `https://bean.ithos.mx/legal/privacy/` |
| Términos de servicio | `https://bean.ithos.mx/legal/terms/` |
| Soporte | `https://bean.ithos.mx/legal/support/` |
| Instrucciones de borrado de datos | `https://bean.ithos.mx/legal/data-deletion/` |
| Callback de solicitud de borrado (Meta) | `https://bean.ithos.mx/legal/data-deletion/callback/` |
| Callback de desautorización (Meta) | `https://bean.ithos.mx/legal/meta/deauthorize/` |
| Página para revisores | `https://bean.ithos.mx/review/` |
| Dominio de la app | `bean.ithos.mx` |

El código arma el redirect URI con `request.build_absolute_uri(reverse("social_accounts:oauth_callback", platform=…))`, es decir `https://bean.ithos.mx/social-accounts/callback/<slug>/` (debe salir con `https` detrás del proxy de Railway; ver brecha M3).

| Plataforma | OAuth redirect URI a registrar |
|---|---|
| Facebook | `https://bean.ithos.mx/social-accounts/callback/facebook/` |
| Instagram (vía Facebook Login) | `https://bean.ithos.mx/social-accounts/callback/instagram/` |
| TikTok | `https://bean.ithos.mx/social-accounts/callback/social1/` (alias opaco; no contiene «tiktok») |

---

## 1. Meta (Facebook Pages + Instagram Graph API)

Una sola app de Meta (`PLATFORM_FACEBOOK_APP_ID` / `_APP_SECRET`) sirve a Facebook e Instagram. Graph API v25.0. El diálogo OAuth es `https://www.facebook.com/v25.0/dialog/oauth` con `client_id`, `redirect_uri`, `state`, `scope` (separados por coma) y `response_type=code`.

### 1.1 Permisos que BEAN pide hoy

**Facebook** (`providers/facebook.py::required_scopes`): `business_management`, `pages_show_list`, `pages_manage_engagement`, `pages_manage_posts`, `pages_read_engagement`, `pages_read_user_content`, `pages_manage_metadata`, `pages_messaging`; y `read_insights` si Analytics está activo para la plataforma.

**Instagram** (`providers/instagram.py::required_scopes`): `instagram_basic`, `instagram_content_publish`, `instagram_manage_comments`, `instagram_manage_insights`, `pages_show_list`, `pages_read_engagement`.

Meta documenta como mínimo para publicar en Instagram: `instagram_basic`, `instagram_content_publish`, `pages_read_engagement` (+ `pages_show_list`), con token de Página. Para publicar en una Página: `pages_manage_posts` (+ `pages_read_engagement`, `pages_show_list`).

**Recomendación (ver brecha M1):** para la primera revisión pedir solo el conjunto de publicación y dejar inbox/analytics para una segunda ronda.

| Permiso | Mantener en 1.ª revisión | Motivo |
|---|---|---|
| `pages_show_list` | Sí | Listar Páginas administradas |
| `pages_manage_posts` | Sí | Publicar en la Página |
| `pages_read_engagement` | Sí | Dependencia de los anteriores; leer metadatos de la Página |
| `instagram_basic` | Sí | Identificar la cuenta profesional |
| `instagram_content_publish` | Sí | Publicar en Instagram |
| `read_insights`, `instagram_manage_insights` | Opcional (2.ª ronda) | Analytics |
| `pages_manage_engagement`, `pages_read_user_content`, `instagram_manage_comments` | Opcional (2.ª ronda) | Inbox y comentarios |
| `pages_manage_metadata`, `pages_messaging`, `business_management` | No pedir | `pages_messaging` exige mostrar mensajes de Messenger en el screencast; `business_management` y `pages_manage_metadata` no los necesita la publicación |

### 1.2 Justificación por permiso (texto para pegar en el formulario)

**pages_show_list** — "BEAN is an internal tool used by our company to publish on its own Facebook Pages. After the user logs in with Facebook, BEAN calls /me/accounts to list the Pages the user manages, so the user can choose which Page to connect. We do not use this permission for any other purpose."

**pages_manage_posts** — "BEAN publishes content that an authorized member of our team composed and approved inside BEAN (text, photos, videos or links) to the Facebook Page the user connected. The post is created only when the user schedules it or presses Publish now / Process pending. BEAN does not publish anything the user did not write and review."

**pages_read_engagement** — "Required as a dependency of pages_manage_posts and instagram_content_publish. BEAN reads the connected Page's basic information (name, id, picture) and the status of posts it published, to confirm that the post was created and to show the result to the user."

**instagram_basic** — "BEAN reads the username, id and profile picture of the Instagram professional account linked to the connected Facebook Page, so the user can confirm which account they are connecting. The account selection is shown during connection."

**instagram_content_publish** — "BEAN publishes photos, carousels, Reels and Stories to the Instagram professional account that the user connected, using the media and caption composed and approved by the user in BEAN. Publishing happens only when the user schedules or triggers it."

**read_insights / instagram_manage_insights (si se mantienen)** — "BEAN shows the account owner basic performance metrics (reach, impressions, engagement) of the Pages / Instagram account they connected, inside an Analytics screen, so they can see how their own posts performed."

**pages_manage_engagement / pages_read_user_content / instagram_manage_comments (si se mantienen)** — "BEAN's Inbox lets the account owner read and reply to comments on their own posts from one screen. Comments are shown only to authorized team members and are not used for any other purpose."

### 1.3 Requisitos de la cuenta de la app

- App de tipo Business con el caso de uso de gestionar/publicar en Páginas e Instagram.
- Facebook Login (idealmente **Facebook Login for Business** con configuración; ver brecha M2). Registrar los dos redirect URIs de la tabla (el `redirect_uri` del canje de código debe ser idéntico al del diálogo).
- Ajustes básicos: URL de política de privacidad, términos, **Data Deletion Request URL** (`/legal/data-deletion/callback/`) o URL de instrucciones, categoría, ícono 1024×1024, dominio de la app.
- **Deauthorize Callback URL** = `/legal/meta/deauthorize/`.
- Borrado: Meta hace POST con `signed_request` (HMAC-SHA256 con el app secret) y espera JSON `{"url": "<estado>", "confirmation_code": "<código>"}` por HTTPS. Debe borrarse lo obtenido de Facebook y la política debe explicar cómo pedir el borrado.
- **Business Verification:** obligatoria para Advanced Access (vincular la app a un Business Portfolio verificado de ITHOS: documentos legales, dominio verificado, contacto). Sin Advanced Access la app solo funciona con usuarios con rol en ella (Admin/Developer/Tester), lo que **basta para uso interno de una persona**. Decidir si de verdad hace falta App Review (brecha M7).
- **Data Use Checkup** anual y preguntas de manejo de datos.
- Instagram: cuenta profesional ligada a una Página. Si la Página requiere Page Publishing Authorization (PPA), debe completarse antes de publicar.
- Límite documentado: 100 publicaciones por API por cuenta cada 24 h (un carrusel cuenta como una; la propia página menciona 50 en la sección de carruseles: verificar). Imágenes JPEG.

### 1.4 Usuario de prueba y acceso del revisor

- Un usuario de BEAN con rol de editor en un workspace de demostración. Correo y contraseña van en el campo «Notes» del formulario (no en este repo).
- Una Página de Facebook de prueba y una cuenta de Instagram profesional ligada a ella, con el usuario de prueba como administrador. Añadir al revisor como Tester de la app o usar un usuario de prueba de Meta.
- Si Meta no puede acceder a la app, la solicitud completa se rechaza. Comprobar la víspera que `https://bean.ithos.mx/` responde y que el login funciona.
- Instrucciones para el revisor: "Log in at https://bean.ithos.mx/ -> Social accounts -> Connect -> Facebook. Select the test Page. Then Compose -> select the Page and the Instagram account -> Publish now -> Organization settings -> Process pending."

### 1.5 Guion de screencast (Meta) — en inglés

Grabar en 1080p, narrado o con subtítulos, navegador con la URL visible, una toma continua de 4–6 minutos. Facebook en inglés.

1. Open `https://bean.ithos.mx/`. Show the landing page and the footer links: Privacy Policy, Terms, Data deletion. (0:20)
2. Log in as the test user. Show the dashboard and the "Demo" workspace. (0:15)
3. Go to Social accounts -> Connect -> Facebook. Show the URL changes to facebook.com. (0:15)
4. On the Facebook consent dialog, **pause and show every permission requested** (zoom in), choose the test Page, press Continue. (0:40)
5. Back in BEAN, show the account-selection screen with the Pages list and select the test Page. Repeat for Instagram: show the Instagram professional account linked to the Page in the list. (0:40)
6. Go to Compose. Select the Facebook Page and the Instagram account. Type a caption, attach a JPEG image. Show the preview. (0:40)
7. Press **Publish now** (or schedule it, then go to Organization settings -> **Process pending** and press it). Show the status become Published in Publish -> Sent. (0:40)
8. Open the Facebook Page in another tab and show the post live; open Instagram and show the post on the profile. (0:30)
9. If analytics/inbox permissions are requested: open Analytics for the Page and show the metrics; open Inbox and show a comment with a reply sent from BEAN. (0:40)
10. Disconnect: Social accounts -> Disconnect on the Page. Show it disappears. (0:15)
11. Show `https://bean.ithos.mx/legal/data-deletion/`, explain the deletion request and show the status page. In Facebook (Settings -> Apps and Websites) show the Remove app action that triggers the deauthorize callback. (0:30)

---

## 2. TikTok (Login Kit + Content Posting API — Direct Post)

Credenciales: `PLATFORM_TIKTOK_CLIENT_KEY`, `PLATFORM_TIKTOK_CLIENT_SECRET`. Autorización en `https://www.tiktok.com/v2/auth/authorize/`; API en `https://open.tiktokapis.com/v2`.

### 2.1 Productos y scopes

Productos en el portal: **Login Kit** y **Content Posting API** (Direct Post activado). Scopes que BEAN pide (`providers/tiktok.py::required_scopes`): `user.info.basic`, `video.publish`; y `video.list` solo si Analytics está activo para TikTok. Para la revisión pedir solo los dos primeros.

| Scope | Justificación (texto para pegar) |
|---|---|
| `user.info.basic` | "BEAN uses Login Kit so that an authorized member of our company can connect their own TikTok account. We read the creator's open_id, display name and avatar to show which account is connected and to display 'Posting to @nickname' on the post screen." |
| `video.publish` | "BEAN uses the Content Posting API (Direct Post) to publish videos that the connected creator composed in BEAN. Before every post BEAN queries creator_info, shows the creator's nickname, lets the user pick the privacy level (no default), toggle comment/duet/stitch (all off by default), declare commercial content, and read TikTok's consent declaration. The video is sent to TikTok only after the user explicitly confirms the upload. BEAN adds no watermark or promotional text." |
| `video.list` (opcional) | "BEAN reads view, like, comment and share counts of the videos it published for the connected account and shows them in an Analytics screen." |

Descripción del producto Content Posting API: "Internal company tool. Our team composes a video, chooses TikTok privacy and interaction settings and publishes it to our own TikTok accounts. Not offered to third parties."

### 2.2 Requisitos de la app y revisión

- Verificar **Terms of Service URL**, **Privacy Policy URL** y **Web URL** (por dominio o por prefijo de URL con archivo de firma). BEAN ya sirve un archivo de verificación en la raíz (`/tiktok<token>.txt`, `apps/accounts/urls_root.py`). Con Content Posting API esta verificación es obligatoria.
- Redirect URI: `https://bean.ithos.mx/social-accounts/callback/social1/`.
- En «App review», explicar cómo se usa cada producto y scope (textos de §2.1).
- Subir **al menos un video de demostración** del flujo completo (hasta 5 videos, 50 MB cada uno).
- Mientras la app **no esté auditada**: todo el contenido queda `SELF_ONLY`, la cuenta debe ser privada al publicar y solo ~5 usuarios pueden publicar por ventana de 24 h. BEAN lo detecta (`UNAUDITED_CLIENT_HINT`) y bloquea la opción pública y el contenido de marca.
- Sandbox: sirve para probar sin revisión con usuarios de prueba (Target Users).

### 2.3 Requisitos de UX de Direct Post y estado en BEAN

Regla oficial (Content Sharing Guidelines) frente a lo que hace BEAN hoy (`templates/composer/compose.html`, `providers/tiktok.py`).

| Requisito | Estado |
|---|---|
| Consultar `creator_info` fresco cada vez que se muestra la página de publicar | Cumple (`/compose/tiktok-creator-info/<acc>/`; el proveedor la reconsulta al publicar, sin caché) |
| Mostrar el nickname del creador | Cumple («Posting to @nickname») |
| Si el creador no puede publicar ahora, detener y avisar | **Brecha T1**: si `creator_info` falla, el proveedor lo ignora y continúa (`_check_creator_constraints`) |
| Verificar duración contra `max_video_post_duration_sec` | Cumple en el proveedor; **brecha T2**: avisar ya en el composer |
| Título editable | Cumple |
| Privacidad = `privacy_level_options`, sin valor por defecto | Cumple (lista personalizada vacía al inicio, campo requerido) |
| Comentar/Dúo/Stitch manuales, apagados por defecto; deshabilitar si el creador los desactivó | Cumple |
| Divulgación comercial: apagada por defecto; «Your brand» / «Branded content»; bloquear publicar si no se elige | Cumple |
| Textos «Your photo/video will be labeled as 'Promotional content' / 'Paid partnership'» | Cumple (`ttLabelPrompt`); verificar literal |
| Branded content incompatible con «only me», con tooltip «Branded content visibility cannot be set to private.» | Cumple |
| Declaración «By posting, you agree to TikTok's [Branded Content Policy and] Music Usage Confirmation» con enlaces | Cumple |
| Vista previa del contenido antes de publicar | **Brecha T3**: existe `composer/partials/preview_panel.html`; confirmar que muestra video/portada y título de TikTok |
| Sin marcas, logos, marcas de agua ni texto promocional añadido | Cumple |
| Consentimiento expreso antes de enviar a TikTok | **Brecha T4**: en envíos programados el consentimiento es el botón de programar; añadir texto explícito |
| Avisar que el procesamiento puede tardar unos minutos | Cumple |
| Mostrar el progreso (status fetch o webhooks) | **Brecha T5**: el proveedor tiene `/post/publish/status/fetch/`; confirmar que la UI muestre procesando/publicado |
| App sin auditar: solo `SELF_ONLY` | Cumple (aviso en la UI) |
| Redirect URI sin «tiktok» | Cumple (`social1`) |
| Revocar token al desconectar | Cumple (`revoke_token`) |

### 2.4 Usuario de prueba

- Cuenta de TikTok de prueba añadida como Target User del sandbox. Credenciales de BEAN para el revisor en el campo de notas (no en el repo).
- En el video, usar una cuenta **privada** si la app aún no está auditada.

### 2.5 Guion de screencast (TikTok) — en inglés

Pantalla completa del navegador con la URL visible, UI real de BEAN, flujo completo sin ediciones que oculten pasos. Máx. 50 MB por video.

1. Open `https://bean.ithos.mx/`: show the landing page and the links to Privacy Policy and Terms. (0:15)
2. Log in. Go to Social accounts -> Connect -> TikTok. (0:15)
3. TikTok authorization page: show the app name and **every scope requested** (user.info.basic, video.publish). Press Authorize. (0:30)
4. Back in BEAN: show the connected account with the creator's name and avatar. (0:10)
5. Compose -> select the TikTok account -> attach an MP4. Show the page displays **"Posting to @nickname"** fetched from TikTok. (0:30)
6. Show the title field and edit it. Open the privacy dropdown: **no option is preselected**; show the options come from TikTok; select one ("Only you" if unaudited). (0:30)
7. Show Comment / Duet / Stitch are all **unchecked by default**; check Comment. Show an option disabled in the creator's TikTok settings is greyed out. (0:25)
8. Turn on "Disclose post content". Show publishing is blocked until "Your brand" or "Branded content" is chosen; choose Branded content and show the "Paid partnership" label text, the disabled "Only you" option with its tooltip, and the declaration with the Branded Content Policy and Music Usage Confirmation links. Turn disclosure off again for the actual post. (0:50)
9. Show the **preview** of the video with the title. Press Publish now (explicit consent). Show the "it may take a few minutes to process" notice and the status move from processing to published. (0:40)
10. If scheduling is demonstrated: schedule for later, then Organization settings -> **Process pending** and show the post goes out. (0:30)
11. Open TikTok and show the video on the profile with the chosen settings. (0:30)
12. If `video.list` is requested: open Analytics for the TikTok account and show the video metrics. (0:20)
13. Disconnect the account in BEAN, then open `/legal/data-deletion/` and explain how to request deletion. (0:30)

---

## 3. Lista de verificación previa al envío

- [ ] `https://bean.ithos.mx/` responde 200 con HTTPS válido.
- [ ] Privacidad, términos, soporte y borrado públicos, mencionando cada plataforma y los permisos.
- [ ] Callbacks de borrado y desautorización responden con la firma correcta (probar con el botón de prueba del panel de Meta).
- [ ] Login funcionando para el revisor.
- [ ] Redirect URIs idénticos a los de §0.
- [ ] Sin logos de BrightBean visibles.
- [ ] Screencasts grabados con los guiones de §1.5 y §2.5.

## 4. Fuera de esta revisión

YouTube/Google tiene su propio proceso (verificación de OAuth para scopes sensibles y auditoría de la API de YouTube para subir videos públicos). Se documentará aparte.

## 5. Brechas a cerrar antes de enviar

| # | Brecha | Archivo a tocar |
|---|---|---|
| M1 | Se piden más permisos de los necesarios para publicar (`business_management`, `pages_manage_metadata`, `pages_messaging`, `pages_read_user_content`, `pages_manage_engagement`; en Instagram `instagram_manage_comments` e `instagram_manage_insights`). Cada uno exige justificación y screencast propios. Recortar a §1.1 o hacerlo configurable por env | `providers/facebook.py::required_scopes`, `providers/instagram.py::required_scopes` |
| M2 | OAuth con Facebook Login clásico (`scope=`) en vez de Facebook Login for Business (`config_id`); la guía actual de Instagram lo menciona. Evaluar si aplica | `providers/facebook.py::get_auth_url`, `providers/instagram.py::get_auth_url` |
| M3 | Confirmar que el redirect URI sale con `https` tras el proxy y coincide exacto con el registrado | `apps/social_accounts/views.py::_build_redirect_uri`, `config/settings/base.py` |
| M4 | `/legal/privacy/` ya cubre Meta y TikTok (datos, retención y borrado). Pendiente: `/review/` no menciona Meta/Instagram (solo TikTok); añadirlo | `templates/public/review.html` |
| M5 | Callbacks de borrado/desautorización ya implementados (`signed_request` verificado con HMAC-SHA256, responden `{url, confirmation_code}`). Limitación: comparan con `account_platform_id` (id de Página / IG business), no con el app-scoped user id de Meta, por lo que rara vez coinciden. Pendiente: guardar ese user id al conectar y comparar con él | `apps/accounts/views.py`, `apps/social_accounts/views.py` |
| M6 | El screencast de `instagram_basic` debe mostrar la selección de cuenta; confirmar que `select_account` lista Página + Instagram | `templates/social_accounts/account_select.html` |
| M7 | Decidir si hace falta App Review: con un solo usuario con rol en la app no se exige. Si se opta por Advanced Access, hace falta Business Verification | (decisión; sin archivo) |
| T1 | HECHO: si `creator_info` falla, el proveedor aborta con `PublishError` (reintentable) y mensaje visible; test en `tests/providers/test_tiktok.py` | `providers/tiktok.py::_check_creator_constraints` |
| T2 | HECHO (parcial): el composer muestra «TikTok acepta videos de hasta N s en esta cuenta» junto al selector (el cliente no conoce la duración del medio); el proveedor ya bloquea al publicar si `MediaAsset.duration` excede el límite | `templates/composer/compose.html`, `apps/composer/views.py::tiktok_creator_info` |
| T3 | HECHO (ya existía): `preview_panel.html` muestra video y caption de TikTok | `templates/composer/partials/preview_panel.html` |
| T4 | HECHO: párrafo `#tiktok-consent` sobre la barra de acciones (aplica a publicar y programar) con enlaces a Music Usage Confirmation y Branded Content Policy; test en `apps/composer/tests/test_tiktok_consent.py` | `templates/composer/compose.html` |
| T5 | PENDIENTE: el engine no usa `_fetch_publish_status` (solo analytics); un estado «procesando → publicado» exige estado intermedio en `PlatformPost` (migración) y sondeo en el engine. Hoy el composer solo avisa que puede tardar unos minutos | `apps/publisher/engine.py`, `templates/calendar/partials/` |
| T6 | El token de `/tiktok<token>.txt` debe coincidir con el del portal; verificar dominio `ithos.mx` o prefijo y registrar el redirect `social1` | `apps/accounts/urls_root.py` |
| T7 | `publish_per_day=5` en el `RateLimitConfig` de TikTok limita a 5 publicaciones diarias; confirmar que basta tras la auditoría | `providers/tiktok.py` |
| G1 | La UI está pasando a español; el screencast para los revisores debe llevar subtítulos en inglés | (decisión) |
