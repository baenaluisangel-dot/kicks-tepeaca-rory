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


ARCHIVO_CATALOGO_PDF = "Catalogo_Juguetes_Kicks_Tepeaca.pdf"


def url_catalogo_pdf() -> str | None:
    """URL publica del PDF del catalogo de juguetes (vive en static/catalogo)."""
    base = url_base()
    if not base:
        return None
    return f"{base}/catalogo/{ARCHIVO_CATALOGO_PDF}"


ARCHIVO_PAQUETES_PDF = "Catalogo_Paquetes_ToyStory_Kicks.pdf"


def url_paquetes_pdf() -> str | None:
    """URL publica del PDF de paquetes de la campana de Puebla."""
    base = url_base()
    if not base:
        return None
    return f"{base}/catalogo/{ARCHIVO_PAQUETES_PDF}"


# Paquetes de la campana de Puebla: de que juguetes (ids del catalogo) se compone cada uno.
PAQUETES_PUEBLA: dict[str, list[str]] = {
    "Dúo Espacial": ["toy-story-duo-buzz-alien"],
    "Clásico + Alien": ["toy-story-duo-woody-y-buzz", "toy-story-alien-17-cm"],
    "Jessie + Tiro al Blanco": ["toy-story-jessie-con-sonido", "toy-story-tiro-al-blanco-peluche"],
    "Clásico + Rex": ["toy-story-duo-woody-y-buzz", "toy-story-rex-que-habla"],
    "Vaqueros + Rex": [
        "toy-story-woody-de-tela-que-habla", "toy-story-jessie-con-sonido", "toy-story-rex-que-habla",
    ],
    "Aventura sobre ruedas": [
        "toy-story-woody-de-tela-que-habla", "toy-story-buzz-con-alas",
        "toy-story-carro-control-remoto-chico",
    ],
    "Los Héroes": [
        "toy-story-woody-de-tela-que-habla", "toy-story-jessie-con-sonido", "toy-story-buzz-con-alas",
    ],
    "Aventura sobre ruedas XL": [
        "toy-story-woody-de-tela-que-habla", "toy-story-buzz-con-alas",
        "toy-story-carro-control-remoto-grande",
    ],
    "Colección Completa": [
        "toy-story-woody-de-tela-que-habla", "toy-story-jessie-con-sonido",
        "toy-story-buzz-con-alas", "toy-story-rex-que-habla", "toy-story-alien-17-cm",
        "toy-story-tiro-al-blanco-peluche",
    ],
}
