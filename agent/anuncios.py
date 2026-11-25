# agent/anuncios.py — De que anuncio viene cada cliente

"""
Cuando alguien escribe desde un anuncio, Meta agrega los datos del anuncio al primer
mensaje y Zernio los entrega en metadata.ad y metadata.referral. Los nombres de los
campos cambian segun el canal, asi que se buscan varios y ademas se guarda todo crudo.
"""

import json

# En orden de preferencia
_ID = ("adId", "ad_id", "ctwa_source_id", "sourceId", "source_id")
_TITULO = ("headline", "ctwa_headline", "ad_title", "adTitle", "title", "adName", "name")
_TEXTO = ("body", "ctwa_body", "description", "text")
_URL = ("source_url", "sourceUrl", "ctwa_source_url", "video_url", "videoUrl", "photo_url",
        "photoUrl", "image_url", "imageUrl", "url")


def _aplanar(dato, salida: list) -> list:
    """Junta el diccionario y los que vengan anidados (ej. ads_context_data)."""
    if isinstance(dato, dict):
        salida.append(dato)
        for valor in dato.values():
            _aplanar(valor, salida)
    return salida


def _primero(dicts: list, claves: tuple) -> str:
    for clave in claves:
        for d in dicts:
            valor = d.get(clave)
            if isinstance(valor, (str, int)) and str(valor).strip():
                return str(valor).strip()
    return ""


def extraer_anuncio(metadata) -> dict | None:
    """Los datos del anuncio que trae un mensaje, o None si no trae."""
    if not isinstance(metadata, dict):
        return None
    ad, referral = metadata.get("ad"), metadata.get("referral")
    if not ad and not referral:
        return None
    dicts = _aplanar(ad, []) + _aplanar(referral, [])
    anuncio_id = _primero(dicts, _ID)
    if not anuncio_id and isinstance(ad, dict):
        anuncio_id = str(ad.get("id") or "")
    return {
        "anuncio_id": anuncio_id[:100],
        "titulo": _primero(dicts, _TITULO)[:300],
        "texto": _primero(dicts, _TEXTO)[:1000],
        "url": _primero(dicts, _URL)[:1000],
        "datos": json.dumps({"ad": ad, "referral": referral}, ensure_ascii=False, default=str)[:4000],
    }


def campos(metadata) -> str:
    """Solo los NOMBRES de los campos del anuncio, para el diagnostico en los registros."""
    partes = []
    for nombre in ("ad", "referral"):
        dato = (metadata or {}).get(nombre)
        if isinstance(dato, dict):
            nombres = []
            for k, v in dato.items():
                nombres.append(f"{k}{sorted(v.keys())}" if isinstance(v, dict) else k)
            partes.append(f"{nombre}: {sorted(nombres)}")
    return " | ".join(partes)
