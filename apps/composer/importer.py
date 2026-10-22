"""Importador por publicación: un .zip (``bean.json`` v1 + medios) -> un borrador del composer.

El zip es una frontera de confianza: se valida todo (rutas, tamaños, esquema, tipos reales) ANTES de crear nada.
Los errores salen como ``PackageError`` con título y mensaje en español.
"""

from __future__ import annotations

import io
import json
import math
import posixpath
import re
import stat
import zipfile
from dataclasses import dataclass, field

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile, TemporaryUploadedFile
from django.db import transaction

from apps.common.validators import parse_and_truncate_youtube_tag_string
from apps.media_library.models import MediaAsset
from apps.media_library.quotas import StorageQuotaExceededError, enforce_storage_quota
from apps.media_library.validators import ALL_ALLOWED_MIMES, MAX_FILE_SIZES, determine_file_type, sniff_mime
from providers.youtube import YOUTUBE_CATEGORY_IDS

from .models import PlatformPost, Post, PostMedia
from .services import sync_post_scheduled_at

NETS = ("youtube", "instagram", "facebook", "tiktok")
NET_NAMES = {"youtube": "YouTube", "instagram": "Instagram", "facebook": "Facebook", "tiktok": "TikTok"}
#: Plataformas de BEAN que atiende cada red del paquete.
NET_PLATFORMS = {
    "youtube": ("youtube",),
    "instagram": ("instagram", "instagram_login"),
    "facebook": ("facebook",),
    "tiktok": ("tiktok",),
}
MUSICA_MODOS = ("en_app", "libre_en_mp4")
TIPOS = ("video_vertical", "video_horizontal", "imagen", "carrusel")
#: Red cuyo texto es la descripción común del post; las demás solo guardan override si difiere.
CAPTION_ORDER = ("instagram", "facebook", "tiktok", "youtube")
_YT_SHORT_TIPOS = ("video_vertical", "carrusel")
_ID_RE = re.compile(r"^[\w.\-]{1,100}$")
_IG_USER_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
_EXT_KIND = {".mp4": "video", ".mov": "video", ".webm": "video", ".avi": "video",
             ".jpg": "image", ".jpeg": "image", ".png": "image", ".webp": "image", ".gif": "image"}  # fmt: skip
_CHUNK = 1024 * 1024
_MANIFEST_MAX = 2 * 1024 * 1024


class PackageError(Exception):
    def __init__(self, titulo: str, mensaje: str):
        super().__init__(f"{titulo} {mensaje}")
        self.titulo, self.mensaje = titulo, mensaje


def _limit(name: str, default: int) -> int:
    return int(getattr(settings, name, default))


# ---------------------------------------------------------------------------
# Zip
# ---------------------------------------------------------------------------


def open_package(fileobj) -> zipfile.ZipFile:
    """Abre el zip y rechaza tamaños, rutas y tipos de entrada peligrosos (zip slip, symlinks, bombas)."""
    max_zip = _limit("BEAN_IMPORT_MAX_ZIP_BYTES", 1024**3)
    if (getattr(fileobj, "size", 0) or 0) > max_zip:
        raise PackageError(
            "El paquete pesa demasiado.", f"El máximo es {max_zip // 1024**2} MB. Quita medios o comprímelos."
        )
    try:
        zf = zipfile.ZipFile(fileobj)
        infos = zf.infolist()
    except (zipfile.BadZipFile, OSError, ValueError, NotImplementedError) as exc:
        raise PackageError(
            "No pudimos abrir el archivo.",
            "El archivo está dañado o no es un .zip. Vuelve a exportarlo y súbelo otra vez.",
        ) from exc
    if len(infos) > _limit("BEAN_IMPORT_MAX_FILES", 300):
        raise PackageError(
            "El paquete trae demasiados archivos.", f"El máximo es {_limit('BEAN_IMPORT_MAX_FILES', 300)}."
        )
    total = 0
    for i in infos:
        name = i.filename
        if name.startswith(("/", "\\")) or "\\" in name or ".." in name.split("/") or re.match(r"^[A-Za-z]:", name):
            raise PackageError(
                "El paquete trae rutas inseguras.", f"«{name}» sale de la carpeta del paquete. Vuelve a exportarlo."
            )
        if stat.S_ISLNK(i.external_attr >> 16):
            raise PackageError(
                "El paquete trae enlaces simbólicos.", f"«{name}» es un enlace; no se admiten. Incluye el archivo real."
            )
        if i.flag_bits & 0x1:
            raise PackageError(
                "El paquete está protegido con contraseña.", "Expórtalo sin contraseña y súbelo otra vez."
            )
        total += i.file_size
    if total > _limit("BEAN_IMPORT_MAX_UNCOMPRESSED_BYTES", 2 * 1024**3):
        raise PackageError("El paquete descomprimido es demasiado grande.", "Reduce los medios y vuelve a armarlo.")
    return zf


def _read_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo, dest=None, cap: int | None = None):
    """Lee un miembro por trozos; corta si descomprime más que lo declarado (bomba con cabecera mentirosa)."""
    cap = info.file_size if cap is None else cap
    size, parts = 0, []
    with zf.open(info) as src:
        while chunk := src.read(_CHUNK):
            size += len(chunk)
            if size > cap:
                raise PackageError(
                    "El paquete trae un archivo corrupto.", f"«{info.filename}» pesa más de lo que declara."
                )
            if dest is not None:
                dest.write(chunk)
            else:
                parts.append(chunk)
    return b"".join(parts) if dest is None else size


# ---------------------------------------------------------------------------
# Esquema de bean.json
# ---------------------------------------------------------------------------


def _bad(path: str, why: str) -> PackageError:
    return PackageError("bean.json tiene un campo inválido.", f"{path}: {why}.")


def _typed(obj: dict, path: str, spec: dict[str, str]) -> None:
    """Valida tipos de los campos presentes. kinds: str, bool, num, strs (lista de str)."""
    for key, kind in spec.items():
        v = obj.get(key)
        if v is None:
            continue
        ok = {
            "str": isinstance(v, str),
            "bool": isinstance(v, bool),
            "num": isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0,
            "strs": isinstance(v, list) and all(isinstance(x, str) for x in v),
        }[kind]
        if not ok:
            raise _bad(f"{path}.{key}", {"str": "debe ser texto", "bool": "debe ser true o false",
                                         "num": "debe ser un número", "strs": "debe ser una lista de textos"}[kind])  # fmt: skip


_NET_SPEC = {
    "youtube": {"titulo": "str", "descripcion": "str", "hashtags": "strs", "tags": "strs", "categoria_id": "str",
                "hecho_para_ninos": "bool", "contiene_ia": "bool", "miniatura": "str", "primer_comentario": "str"},
    "instagram": {"tipo": "str", "descripcion": "str", "hashtags": "strs", "portada_seg": "num", "portada_ruta": "str",
                  "colaboradores": "strs", "compartir_en_feed": "bool", "primer_comentario": "str"},
    "facebook": {"tipo": "str", "descripcion": "str", "hashtags": "strs", "enlace": "str", "primer_comentario": "str"},
    "tiktok": {"tipo": "str", "titulo": "str", "musica_sugerida": "str", "descripcion": "str", "hashtags": "strs",
               "portada_seg": "num", "permitir_comentarios": "bool", "permitir_duo": "bool", "permitir_stitch": "bool",
               "contenido_marca": "bool", "primer_comentario": "str"},
}  # fmt: skip
_NET_TIPOS = {
    "instagram": {"reel", "feed", "carrusel", "story"},
    "facebook": {"reel", "feed", "album"},
    "tiktok": {"video", "fotos"},
}


def _ruta(m, path: str) -> str:
    r = m.get("ruta") if isinstance(m, dict) else m
    if not isinstance(r, str) or not r.strip():
        raise _bad(path, "debe ser una ruta (texto) o un objeto con «ruta»")
    return r


def _validate_piece(p, idx: int) -> dict:
    path = f"piezas[{idx}]"
    if not isinstance(p, dict):
        raise _bad(path, "debe ser un objeto")
    if not isinstance(p.get("id"), str) or not _ID_RE.match(p["id"]):
        raise _bad(f"{path}.id", "falta o no es un identificador válido (letras, números, . _ -)")
    if p.get("tipo") not in TIPOS:
        raise _bad(f"{path}.tipo", f"debe ser uno de {', '.join(TIPOS)}")
    medios = p.get("medios")
    if not isinstance(medios, list) or not medios:
        raise _bad(f"{path}.medios", "debe traer al menos un medio")
    p["_medios"] = [_ruta(m, f"{path}.medios[{i}]") for i, m in enumerate(medios)]
    p["_meta"] = {r: m for r, m in zip(p["_medios"], medios, strict=True) if isinstance(m, dict)}
    for r, m in p["_meta"].items():
        _typed(m, f"{path}.medios[{r}]", {"ancho": "num", "alto": "num", "duracion_s": "num"})
    mpr = p.get("medios_por_red") or {}
    if not isinstance(mpr, dict):
        raise _bad(f"{path}.medios_por_red", "debe ser un objeto {red: [rutas]}")
    for red, lst in mpr.items():
        if red not in NETS or not isinstance(lst, list) or not lst or not all(isinstance(x, str) for x in lst):
            raise _bad(f"{path}.medios_por_red.{red}", "red desconocida o lista de rutas vacía")
    _typed(p, path, {"solo_organico": "bool", "motivo_organico": "str", "portada_texto": "str", "titulo_alt": "str",
                     "portada_ruta": "str", "portada_seg": "num", "contiene_ia": "bool"})  # fmt: skip
    mus = p.get("musica")
    if mus is not None:
        if not isinstance(mus, dict):
            raise _bad(f"{path}.musica", "debe ser un objeto")
        _typed(mus, f"{path}.musica", {"sugerencia": "str", "golpe_seg": "num", "nota": "str"})
        for red in NETS:
            if mus.get(red) is not None and mus[red] not in MUSICA_MODOS:
                raise _bad(f"{path}.musica.{red}", f"debe ser {' o '.join(MUSICA_MODOS)}")
    redes = p.get("redes")
    if not isinstance(redes, dict) or not redes:
        raise _bad(f"{path}.redes", "debe traer al menos una red")
    for red, d in redes.items():
        if red not in NETS:
            continue  # red que esta versión no conoce: se ignora
        if not isinstance(d, dict):
            raise _bad(f"{path}.redes.{red}", "debe ser un objeto")
        _typed(d, f"{path}.redes.{red}", _NET_SPEC[red])
        if d.get("tipo") is not None and red in _NET_TIPOS and d["tipo"] not in _NET_TIPOS[red]:
            raise _bad(f"{path}.redes.{red}.tipo", f"debe ser uno de {', '.join(sorted(_NET_TIPOS[red]))}")
        if red == "youtube":
            if not isinstance(d.get("hecho_para_ninos"), bool):
                raise _bad(f"{path}.redes.youtube.hecho_para_ninos", "es obligatorio y debe ser true o false")
            caps = d.get("capitulos") or []
            if not isinstance(caps, list) or not all(
                isinstance(c, dict) and isinstance(c.get("t"), str) and isinstance(c.get("titulo"), str) for c in caps
            ):
                raise _bad(f"{path}.redes.youtube.capitulos", "debe ser una lista de {t, titulo}")
        if red == "tiktok" and d.get("tipo") == "fotos" and len(d.get("titulo") or "") > 90:
            raise _bad(f"{path}.redes.tiktok.titulo", "máximo 90 caracteres")
    return p


def load_manifest(zf: zipfile.ZipFile) -> dict:
    names = {i.filename for i in zf.infolist()}
    if "bean.json" not in names:
        nested = next((n for n in sorted(names) if n.endswith("/bean.json")), None)
        hint = f" Encontramos «{nested}»: comprime el contenido de la carpeta, no la carpeta." if nested else ""
        raise PackageError(
            "El paquete no trae bean.json.", "Debe estar en la raíz del .zip, no dentro de una carpeta." + hint
        )
    try:
        data = json.loads(_read_member(zf, zf.getinfo("bean.json"), cap=_MANIFEST_MAX).decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise PackageError("bean.json no se puede leer.", "No es un JSON válido (UTF-8). Vuelve a exportarlo.") from exc
    if not isinstance(data, dict):
        raise PackageError("bean.json no se puede leer.", "Debe ser un objeto JSON.")
    if data.get("version") != 1:
        raise PackageError(
            f"bean.json es de la versión {data.get('version')!s:.20}.",
            "Esta versión de BEAN entiende la versión 1. Actualiza BEAN o exporta el paquete con el formato versión 1.",
        )
    if not isinstance(data.get("proyecto"), str) or not data["proyecto"].strip():
        raise _bad("proyecto", "falta o no es texto")
    _typed(data, "", {"marca": "str", "espacio_sugerido": "str"})
    piezas = data.get("piezas")
    if not isinstance(piezas, list) or not piezas:
        raise _bad("piezas", "debe traer al menos una publicación")
    data["piezas"] = [_validate_piece(p, i) for i, p in enumerate(piezas)]
    ids = [p["id"] for p in data["piezas"]]
    if len(set(ids)) != len(ids):
        raise _bad("piezas", "hay ids repetidos")
    return data


def piece_summary(piece: dict) -> dict:
    return {"id": piece["id"], "tipo": piece["tipo"], "redes": [NET_NAMES[r] for r in NETS if r in piece["redes"]]}


# ---------------------------------------------------------------------------
# Planeación (cuentas, medios por red) y creación
# ---------------------------------------------------------------------------


@dataclass
class Plan:
    piece: dict
    targets: dict = field(default_factory=dict)  # red -> {"accounts": [...], "media": [rutas]}
    notices: list = field(default_factory=list)
    skipped: list = field(default_factory=list)


def _net_media(piece: dict, net: str) -> list[str]:
    own = (piece.get("medios_por_red") or {}).get(net)
    return list(own) if own else list(piece["_medios"])


#: TikTok (modo fotos) solo admite JPEG y WEBP; el resto de imágenes se convierte a JPEG al importar.
_TT_PHOTO_MIMES = ("image/jpeg", "image/webp")
_JPG = "#jpg"  # JPEG convertido: lo comparten TikTok fotos e Instagram


def _mime(zf, path: str) -> str:
    with zf.open(path) as f:
        return sniff_mime(io.BytesIO(f.read(32))) or ""


def _kind(zf, path: str) -> str:
    """Tipo real (image/video) por magic bytes; falla si falta el archivo o no coincide con su extensión."""
    try:
        info = zf.getinfo(path)
    except KeyError:
        raise PackageError(
            "Falta un archivo del paquete.",
            f"La pieza usa «{path}» y no está en el .zip. Agrégalo en esa ruta (o corrige «ruta» en bean.json) y súbelo otra vez.",
        ) from None
    if info.is_dir():
        raise PackageError("Falta un archivo del paquete.", f"«{path}» es una carpeta, no un archivo.")
    with zf.open(info) as f:
        head = f.read(32)

    mime = sniff_mime(io.BytesIO(head))
    ftype = determine_file_type(mime) if mime in ALL_ALLOWED_MIMES else None
    if not ftype:
        raise PackageError("Un medio no es del tipo que dice.", f"«{path}» no es una imagen ni un video reconocido.")
    ext_kind = _EXT_KIND.get(posixpath.splitext(path)[1].lower())
    real = "video" if ftype == "video" else "image"
    if ext_kind and ext_kind != real:
        raise PackageError(
            "Un medio no es del tipo que dice.", f"«{path}» dice ser {ext_kind} pero por dentro es {real}."
        )
    if info.file_size > MAX_FILE_SIZES.get(ftype, 20 * 1024 * 1024):
        mb = MAX_FILE_SIZES.get(ftype, 20 * 1024 * 1024) // 1024**2
        raise PackageError(
            "Un medio pesa más de lo permitido.",
            f"«{path}» pesa {info.file_size / 1024**2:.0f} MB y el máximo es {mb} MB.",
        )
    return real


def plan_import(zf, piece: dict, accounts) -> Plan:
    """Decide qué redes entran, con qué cuentas y medios. Valida los medios del zip."""
    plan = Plan(piece)
    kinds: dict[str, str] = {}
    for net in NETS:
        if net not in piece["redes"]:
            continue
        name = NET_NAMES[net]
        media = _net_media(piece, net)
        if net == "youtube" and piece["tipo"] == "carrusel" and not (piece.get("medios_por_red") or {}).get("youtube"):
            plan.skipped.append(f"{name}: carrusel sin video para YouTube; publícalo a mano desde la app.")
            continue
        for r in media:
            kinds.setdefault(r, _kind(zf, r))
        if net == "youtube" and "video" not in {kinds[r] for r in media}:
            plan.skipped.append(f"{name}: no hay un video para esta red; publícalo a mano desde la app.")
            continue
        accs = [a for a in accounts if a.platform in NET_PLATFORMS[net]]
        if not accs:
            plan.skipped.append(f"{name}: no hay cuentas conectadas en este espacio.")
            continue
        plan.targets[net] = {"accounts": accs, "media": media}
        for r in _cover_paths(piece, net):
            kinds.setdefault(r, _kind(zf, r))
        if net == "tiktok" and piece["redes"][net].get("tipo") == "fotos" and any(kinds[r] != "image" for r in media):
            raise PackageError(
                "TikTok en modo fotos necesita imágenes.", "Revisa medios_por_red.tiktok: trae un video."
            )
    if not plan.targets:
        raise PackageError(
            "No hay a dónde enviar el borrador.",
            "Ninguna red del paquete tiene una cuenta conectada en este espacio. Conecta cuentas o elige otro espacio. "
            + " ".join(plan.skipped),
        )
    plan.notices = list(plan.skipped)
    return plan


def _cover_paths(piece: dict, net: str) -> list[str]:
    """Imágenes de portada/miniatura que usa esta red; la portada_ruta de la pieza sirve a IG y a YouTube largo."""
    d = piece["redes"][net]
    paths = [d.get("miniatura"), d.get("portada_ruta")]
    if net == "instagram" or (net == "youtube" and piece["tipo"] not in _YT_SHORT_TIPOS):
        paths.append(piece.get("portada_ruta"))
    return [r for r in paths if r]


def _musica_en_app(piece: dict, net: str) -> bool:
    """En la app de la red: así lo pide el paquete, o es «libre en mp4» pero esta red no trae medio propio."""
    mode = (piece.get("musica") or {}).get(net)
    return mode == "en_app" or (mode == "libre_en_mp4" and not (piece.get("medios_por_red") or {}).get(net))


def _text(d: dict) -> str:
    desc = (d.get("descripcion") or "").strip()
    tags = [t if t.startswith("#") else f"#{t}" for t in (d.get("hashtags") or []) if t.strip()]
    missing = [t for t in tags if t not in desc]
    return f"{desc}\n\n{' '.join(missing)}".strip() if missing else desc


def _extra(net: str, piece: dict, d: dict, assets: dict, source: dict) -> dict:
    extra: dict = {"import_source": source}
    if piece.get("solo_organico"):
        extra["solo_organico"] = True
    ms = lambda s: int(float(s) * 1000)  # noqa: E731
    ia = bool(piece.get("contiene_ia") or d.get("contiene_ia"))
    if net == "youtube":
        short = piece["tipo"] in _YT_SHORT_TIPOS
        extra |= {"made_for_kids": d["hecho_para_ninos"], "self_declared_made_for_kids": d["hecho_para_ninos"]}
        if tags := parse_and_truncate_youtube_tag_string(",".join(d.get("tags") or [])):
            extra["tags"] = tags
        if str(d.get("categoria_id")) in YOUTUBE_CATEGORY_IDS:
            extra["category_id"] = str(d["categoria_id"])
        if ia:
            extra["contains_synthetic_media"] = True
        if short:
            extra["post_type"] = "short"  # los Shorts no admiten miniatura ni capítulos por API
        else:
            if d.get("capitulos"):
                extra["chapters"] = [{"t": c["t"], "title": c["titulo"][:100]} for c in d["capitulos"]][:50]
            if thumb := d.get("miniatura") or piece.get("portada_ruta"):
                extra["thumbnail_asset_id"] = str(assets[thumb].id)
    elif net == "instagram":
        pt = {"reel": "reel", "story": "story", "carrusel": "carousel"}.get(d.get("tipo") or "")
        if pt:
            extra["post_type"] = pt
        cover = d.get("portada_ruta") or piece.get("portada_ruta")
        seg = d.get("portada_seg", piece.get("portada_seg"))
        if cover:
            extra["ig_cover_asset_id"] = str(assets[cover].id)
        elif seg is not None and pt == "reel":
            extra["thumb_offset"] = ms(seg)  # la imagen manda; el fotograma solo sin portada_ruta
        names = [n.lstrip("@") for n in d.get("colaboradores") or []]
        if collab := [n for n in dict.fromkeys(names) if _IG_USER_RE.match(n)][:3]:
            extra["collaborators"] = collab
        if ia:
            extra["marca_ia_en_app"] = True
        if d.get("compartir_en_feed") is not None:
            extra["share_to_feed"] = d["compartir_en_feed"]
    elif net == "facebook":
        if d.get("tipo") == "reel":
            extra["post_type"] = "reel"
        if ia:
            extra["marca_ia_en_app"] = True
        if re.match(r"^https?://\S{1,2040}$", d.get("enlace") or ""):
            extra["link"] = d["enlace"]
    else:  # tiktok: sin privacidad, la elige el usuario
        extra |= {
            "disable_comment": not d.get("permitir_comentarios", False),
            "brand_content_toggle": bool(d.get("contenido_marca")),
            "brand_organic_toggle": False,
        }
        if ia:
            extra["is_aigc"] = True
        if d.get("tipo") == "fotos":
            extra |= {"post_type": "image", "photo_cover_index": 0}  # el modo fotos no admite duo ni stitch
        else:
            extra |= {
                "disable_duet": not d.get("permitir_duo", False),
                "disable_stitch": not d.get("permitir_stitch", False),
            }
            if (seg := d.get("portada_seg", piece.get("portada_seg"))) is not None:
                extra["video_cover_timestamp_ms"] = ms(seg)
    # Música «en la app»: TikTok llega como borrador (el usuario puede cambiarlo); las demás redes se publican a mano.
    if _musica_en_app(piece, net):
        m = piece["musica"]
        extra["musica"] = {k: m[k] for k in ("sugerencia", "golpe_seg", "nota") if m.get(k) is not None}
        if net == "tiktok":
            extra["tiktok_mode"] = "draft"
        else:
            extra["manual_publish"] = True
    return extra


def _as_jpeg(zf, info, name: str) -> SimpleUploadedFile:
    """Convierte una imagen (PNG, GIF…) a JPEG sobre fondo blanco; falla con un mensaje claro."""
    from PIL import Image, UnidentifiedImageError

    from apps.media_library.services import to_jpeg_bytes

    buf = io.BytesIO()
    _read_member(zf, info, dest=buf)
    try:
        data = to_jpeg_bytes(buf.getvalue())
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise PackageError(
            "No se pudo preparar una imagen.",
            f"«{name}» no se pudo convertir a JPEG (TikTok e Instagram necesitan JPG).",
        ) from exc
    return SimpleUploadedFile(posixpath.splitext(posixpath.basename(name))[0] + ".jpg", data, "image/jpeg")


def _upload(zf, path: str, user, workspace, *, as_jpeg: bool = False) -> MediaAsset:
    from apps.media_library.services import create_asset

    info = zf.getinfo(path)
    if as_jpeg:
        return create_asset(workspace.organization, workspace, _as_jpeg(zf, info, path), user)
    name = posixpath.basename(path)
    tmp = TemporaryUploadedFile(name, "application/octet-stream", info.file_size, None)
    try:
        _read_member(zf, info, dest=tmp.file)
        tmp.seek(0)
        tmp.size = info.file_size
        return create_asset(workspace.organization, workspace, tmp, user)
    finally:
        tmp.close()


def find_existing(workspace, source: dict, may_edit):
    """Borrador (todo en draft) ya importado de la misma pieza, si el usuario puede editarlo."""
    # La clave se filtra en la BD (igual en Postgres y SQLite); el proyecto se compara en Python.
    hits = PlatformPost.objects.filter(
        post__workspace=workspace, platform_extra__import_source__pieza_id=source["pieza_id"]
    ).select_related("post")
    posts = {
        pp.post_id: pp.post
        for pp in hits.order_by("-post__created_at")
        if pp.platform_extra.get("import_source") == source
    }
    for post in posts.values():
        if not post.platform_posts.exclude(status="draft").exists() and may_edit(post):
            return post
    return None


def create_draft(*, workspace, user, zf, plan: Plan, project: str, update: Post | None = None) -> Post:
    """Crea (o reescribe, si ``update``) el borrador. Todo o nada; borra los archivos subidos si algo falla."""
    piece = plan.piece
    source = {"proyecto": project, "pieza_id": piece["id"]}
    paths: list[str] = []
    for t in plan.targets.values():
        paths += t["media"]
    for net in plan.targets:
        paths += _cover_paths(piece, net)
    paths = list(dict.fromkeys(paths))
    tt = plan.targets.get("tiktok")
    photos = bool(tt) and piece["redes"]["tiktok"].get("tipo") == "fotos"
    # Por red: medios que se convierten a JPEG (TikTok fotos: lo que no sea JPEG/WEBP; Instagram: todo lo no JPEG).
    conv_by: dict[str, set[str]] = {}
    for net, t in plan.targets.items():
        if net == "tiktok" and photos:
            conv_by[net] = {p for p in t["media"] if _mime(zf, p) not in _TT_PHOTO_MIMES}
        elif net == "instagram":
            conv_by[net] = {p for p in t["media"] if (m := _mime(zf, p)).startswith("image/") and m != "image/jpeg"}
    conv = [p for p in dict.fromkeys(paths) if any(p in c for c in conv_by.values())]
    shared = {r for n, t in plan.targets.items() for r in t["media"] if r not in conv_by.get(n, ())}
    shared |= set(piece["_medios"]) - set(conv)
    shared |= {r for n in plan.targets for r in _cover_paths(piece, n)}
    paths = [p for p in paths if p not in conv or p in shared]

    def akey(net, r):  # clave del asset de este medio para esta red (JPEG convertido)
        return r + _JPG if r in conv_by.get(net, ()) else r

    try:
        enforce_storage_quota(workspace.organization, sum(zf.getinfo(p).file_size for p in {*paths, *conv}))
    except StorageQuotaExceededError as exc:
        raise PackageError("No cabe en tu almacenamiento.", str(exc)) from exc

    assets: dict[str, MediaAsset] = {}
    try:
        with transaction.atomic():
            for p in paths:
                try:
                    assets[p] = _upload(zf, p, user, workspace)
                except (ValidationError, ValueError) as exc:
                    raise PackageError(
                        "Un medio no se pudo guardar.", f"«{p}»: {'; '.join(getattr(exc, 'messages', [str(exc)]))}"
                    ) from exc
                meta = piece["_meta"].get(p) or {}
                upd = {k: meta[k] for k in ("ancho", "alto", "duracion_s") if isinstance(meta.get(k), (int, float))}
                if upd:
                    assets[p].width, assets[p].height = int(upd.get("ancho", 0)), int(upd.get("alto", 0))
                    assets[p].duration = float(upd.get("duracion_s", 0))
                    assets[p].save(update_fields=["width", "height", "duration"])
            for p in dict.fromkeys(conv):
                assets[p + _JPG] = _upload(zf, p, user, workspace, as_jpeg=True)
            first = {n: _text(piece["redes"][n]) for n in CAPTION_ORDER if n in plan.targets}
            lead = next(iter(first))
            comments = {n: (piece["redes"][n].get("primer_comentario") or "").strip() for n in plan.targets}
            lead_c = next((comments[n] for n in CAPTION_ORDER if n in plan.targets and comments[n]), "")
            yt = piece["redes"].get("youtube") if "youtube" in plan.targets else None
            notes = []
            if piece.get("solo_organico"):
                notes.append(
                    f"Solo orgánico: {piece.get('motivo_organico') or 'sin motivo indicado'}. No lo promociones."
                )
            if piece.get("portada_texto"):
                notes.append(f"Texto de portada sugerido: {piece['portada_texto']}")
            if any(comments.values()):
                notes.append(
                    "Primer comentario: YouTube e Instagram no permiten fijarlo por API; fíjalo a mano tras publicar."
                )
            tt = piece["redes"].get("tiktok") if "tiktok" in plan.targets else None
            if tt and tt.get("musica_sugerida"):
                notes.append(f"Elige la música en la app de TikTok: {tt['musica_sugerida']}")
            fields = {
                "title": ((yt or {}).get("titulo") or piece.get("titulo_alt") or "")[:255],
                "caption": first[lead],
                "first_comment": lead_c,
                "internal_notes": "\n".join(notes),
            }
            if update:
                for k, v in fields.items():
                    setattr(update, k, v)
                update.save()
                update.platform_posts.all().delete()
                update.media_attachments.all().delete()
                post = update
            else:
                post = Post.objects.create(workspace=workspace, author=user, **fields)
            keys = piece["_medios"] + [akey(n, r) for n, t in plan.targets.items() for r in t["media"]]
            order = list(dict.fromkeys(k for k in keys if k in assets))
            for pos, r in enumerate(order):
                PostMedia.objects.create(post=post, media_asset=assets[r], position=pos)
            for net, t in plan.targets.items():
                d = piece["redes"][net]
                text, comment = _text(d), comments[net]
                title = d.get("titulo") if net == "youtube" or (net == "tiktok" and d.get("tipo") == "fotos") else None
                for acc in t["accounts"]:
                    PlatformPost.objects.create(
                        post=post,
                        social_account=acc,
                        platform_specific_title=title,
                        platform_specific_caption=text if text != fields["caption"] else None,
                        platform_specific_first_comment=comment if comment != lead_c else None,
                        platform_specific_media=[str(assets[akey(net, r)].id) for r in t["media"]],
                        platform_extra=_extra(net, piece, d, assets, source),
                    )
            sync_post_scheduled_at(post)
    except Exception:
        for a in assets.values():
            a.file.delete(save=False)
        raise
    return post
