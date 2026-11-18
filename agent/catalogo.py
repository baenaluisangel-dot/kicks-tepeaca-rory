# agent/catalogo.py — Catalogo de productos y fotos
# Generado por AgentKit

"""
Lee el catalogo de config/catalogo.json (id, nombre, precio, foto) y arma la URL publica
de cada foto. Las fotos viven en static/fotos y el servidor las publica en /fotos.
"""

import json
import logging
import os

logger = logging.getLogger("agentkit")

RUTA_CATALOGO = "config/catalogo.json"


def _cargar() -> dict[str, dict]:
    try:
        with open(RUTA_CATALOGO, "r", encoding="utf-8") as f:
            return {p["id"]: p for p in json.load(f)}
    except FileNotFoundError:
        logger.error(f"{RUTA_CATALOGO} no encontrado")
        return {}


PRODUCTOS: dict[str, dict] = _cargar()


def ids_validos() -> list[str]:
    return list(PRODUCTOS.keys())


def obtener_producto(producto_id: str) -> dict | None:
    return PRODUCTOS.get((producto_id or "").strip())


def url_base() -> str:
    """
    URL publica del servidor. WhatsApp tiene que poder descargar la foto desde internet,
    asi que en produccion sale de PUBLIC_BASE_URL o, si no esta, del dominio de Railway.
    """
    base = (os.getenv("PUBLIC_BASE_URL") or "").strip()
    if not base and os.getenv("RAILWAY_PUBLIC_DOMAIN"):
        base = f"https://{os.environ['RAILWAY_PUBLIC_DOMAIN']}"
    return base.rstrip("/")


def url_foto(ruta_foto: str) -> str | None:
    """URL publica de una foto (ruta relativa a static/fotos, ej: 'buzz/01.jpg')."""
    base = url_base()
    if not base or not ruta_foto:
        return None
    return f"{base}/fotos/{ruta_foto}"


def pie_de_foto(producto: dict, numero: int = 1, total: int = 1) -> str:
    texto = f"{producto['nombre']} — ${producto['precio']:,}"
    if total > 1 and numero > 1:
        texto += f" ({numero}/{total})"
    return texto
