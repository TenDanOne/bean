# CORS de R2 para la subida directa

El composer y la biblioteca suben los archivos del navegador directo a R2 (POST prefirmado, con progreso real). Para que el navegador pueda hacerlo hay que poner una regla CORS en el bucket. Si falta, la carga cae sola al camino anterior (pasa por el servidor) pero con el límite de tamaño y la lentitud de siempre.

## Regla (Cloudflare R2 > bucket > Settings > CORS Policy)

```json
[
  {
    "AllowedOrigins": ["https://bean.ithos.mx"],
    "AllowedMethods": ["POST", "PUT", "GET", "HEAD"],
    "AllowedHeaders": ["*"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3600
  }
]
```

- `POST` es el que usa BEAN hoy (la URL prefirmada es un POST con `fields`, igual que las herramientas MCP `request_media_upload`/`finalize_media_upload`). `PUT` queda permitido por si se cambia a URL prefirmada de PUT.
- Para desarrollo local agrega `http://localhost:8000` a `AllowedOrigins`.
- `GET`/`HEAD` hacen falta para que el recorte de fotogramas del video en el composer (canvas) no se bloquee por origen cruzado.

## Variables y CSP

- `STORAGE_BACKEND=s3` activa la subida directa; con almacenamiento local se usa el camino por el servidor.
- `connect-src` del CSP incluye automáticamente el host de `S3_ENDPOINT_URL` y de `S3_CUSTOM_DOMAIN` (`config/settings/base.py`).
- Tras la subida el servidor valida tipo (bytes reales), tamaño y cuota de la organización en `POST .../library/upload/finalize/`; un archivo rechazado se borra del bucket.
