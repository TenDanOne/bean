# Guía de uso — BEAN by ITHOS

Cómo hacer las cosas, por tareas. No hace falta saber programar. La interfaz está en inglés; aquí van los nombres tal como los verás.

## Antes de empezar: cuatro cosas que confunden

1. **Nada sale solo.** BEAN no tiene un proceso encendido todo el día. Un post programado sale cuando tú pulsas **Process pending** (Procesar pendientes) o cuando un cron externo llama a `/cron/run`. Si programas para las 10:00 y procesas a las 12:00, sale a las 12:00.
2. **Organización → Workspace → Cuentas.** La organización es ITHOS; cada workspace es una marca (p. ej. SIMU); dentro cuelgan las cuentas sociales.
3. **Solo cuatro redes:** YouTube, Facebook (Páginas), Instagram (profesional) y TikTok.
4. **Instagram necesita una Página de Facebook.** La cuenta de Instagram debe ser Business o Creator y estar ligada a una Página que administras.

## 1. Entrar

Abre `https://bean.ithos.mx/`, pulsa entrar (login o Google) y acepta los términos la primera vez. Al entrar caes en el panel del último workspace usado.

## 2. Conectar una cuenta

1. Menú **Social accounts** (cuentas sociales) del workspace.
2. **Connect** y elige la plataforma.
3. La plataforma te muestra los permisos que BEAN pide. Léelos y acepta.
4. Si eres administrador de varias Páginas de Facebook, elige cuál conectar en la pantalla de selección.
5. Vuelves a BEAN y la cuenta aparece en la lista. Si dice que necesita reconectar, pulsa **Reconnect**.

TikTok: la cuenta debe ser la tuya o de un usuario de prueba autorizado. Hasta que TikTok audite la app, solo se puede publicar en privado («Only you»).

## 3. Crear un post

1. **Compose**.
2. Marca las cuentas destino. Cada una puede tener su propio texto, medios y ajustes.
3. Escribe el texto, añade imágenes o video (de la biblioteca o subiéndolos).
4. Ajustes por red:
   - **YouTube:** título y visibilidad (Public / Unlisted / Private).
   - **Instagram:** tipo (imagen, carrusel, reel, story). Imágenes JPEG.
   - **TikTok:** solo video. Verás «Posting to @tu_usuario». Debes **elegir** quién puede verlo (no hay valor por defecto), marcar manualmente Comment / Duet / Stitch si quieres permitirlos, y, si el video promociona una marca, activar «Disclose post content» y elegir Your brand o Branded content. Lee la declaración «By posting, you agree to…» antes de enviar.
5. Revisa la **vista previa**.
6. El borrador se guarda solo. También puedes guardarlo como plantilla o clonarlo después para otra marca.

## 4. Aprobar

Si el workspace exige aprobación, el post queda en **Pending review**. Un aprobador va a **Publish → Approvals** y elige **Approve**, **Request changes** (con comentario) o **Reject**. Un post con cambios pedidos vuelve a ti como borrador con el comentario visible.

## 5. Programar

- **Fecha concreta:** en el composer elige fecha y hora y programa.
- **Cola:** define horarios fijos en **Calendar → Posting slots** (p. ej. lunes y jueves 10:00) y crea una **Queue**. Lo que añadas a la cola toma el siguiente hueco libre.
- En el **Calendar** puedes arrastrar un post a otro día.

## 6. Procesar pendientes (publicar)

1. **Organization settings** → tarjeta **Process pending**.
2. Pulsa el botón. BEAN publica lo vencido y hace las demás tareas (sincronizar analytics, refrescar tokens).
3. Mira **Publish → Sent**: `published` es éxito; `failed` muestra el motivo y se reintenta solo (1 min, 5 min, 30 min) la próxima vez que proceses.

**Publicar ahora** (en el composer) hace lo mismo solo para ese post, sin esperar.

Para automatizarlo: un cron externo (p. ej. cron-job.org) con `POST https://bean.ithos.mx/cron/run` y la cabecera `Authorization: Bearer <CRON_SECRET>`. Sin secreto configurado el endpoint responde 503; con uno incorrecto, 401.

## 7. Ver resultados

- **Analytics**: general, por cuenta y por publicación. Se actualiza al procesar pendientes.
- **Inbox**: comentarios y mensajes de las cuentas conectadas; responde, asigna o marca como resuelto.
- Comprueba también en la propia plataforma: TikTok puede tardar unos minutos en mostrar el video.

## 8. Desconectar y borrar datos

- **Disconnect** (en la lista de cuentas) revoca el acceso de BEAN y conserva el historial.
- **Delete** borra la cuenta de BEAN; la pantalla avisa cuántos posts quedan afectados.
- También puedes quitar BEAN desde Facebook (Configuración → Apps y sitios web) o TikTok; BEAN recibe el aviso.
- Para pedir el borrado de todos tus datos: `https://bean.ithos.mx/legal/#borrado-de-datos`.

## 9. Si algo falla

| Síntoma | Qué hacer |
|---|---|
| El post sigue `scheduled` | Pulsa Process pending |
| `failed` con «token» o «scope» | Reconnect de la cuenta |
| TikTok: «can only post to private accounts» | La app aún no pasó la auditoría: publica con «Only you» |
| Instagram: error de Página | Comprueba que la cuenta sea profesional y esté ligada a una Página |
| Instagram rechaza imagen | Debe ser JPEG |
| Soporte | `https://bean.ithos.mx/legal/support/` |
