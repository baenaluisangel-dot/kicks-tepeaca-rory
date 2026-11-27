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


ZONAS_PUEBLA = ["Angelópolis", "Valsequillo", "Peri Plaza", "CAPU", "CU", "Centro",
                "Amozoc", "Chachapa", "Amalucan"]


# Videos de los juguetes Toy Story (static/videos, MP4 H.264). La clave es lo que ve Claude.
VIDEOS_TOY_STORY: dict[str, tuple[str, str]] = {
    "Woody": ("woody.mp4", "Woody (Sheriff Woody que habla)"),
    "Jessie": ("jessie.mp4", "Jessie con sonido"),
    "Buzz Lightyear con alas": ("buzz-lightyear.mp4", "Buzz Lightyear con alas (habla, luces, brilla en la oscuridad)"),
    "Buzz solo": ("buzz-solo.mp4", "Buzz Lightyear (individual)"),
    "Buzz chico": ("buzz-chico.mp4", "Buzz chico (el video incluye una comparativa entre el Buzz grande y el Buzz chico)"),
    "Alien": ("alien.mp4", "Alien (figura de acción)"),
    "Rex": ("rex.mp4", "Rex que habla"),
    "Tiro al Blanco": ("tiro-al-blanco.mp4", "Tiro al Blanco (peluche)"),
    "Carro control remoto chico": ("carro-control-remoto-chico.mp4", "Carro RC chico de Toy Story"),
    "Carro control remoto grande": ("carro-control-remoto-grande.mp4", "Carro RC grande de Toy Story (35 cm)"),
    "Woody y Buzz": ("woody-y-buzz.mp4", "Dúo Woody y Buzz"),
    "Buzz y Alien": ("buzz-y-alien.mp4", "Dúo Buzz y Alien"),
    "Todos los juguetes": ("todos-los-juguetes.mp4", "Todos los juguetes Toy Story juntos"),
}


def url_video(archivo: str) -> str | None:
    base = url_base()
    return f"{base}/videos/{archivo}" if base else None


ARCHIVO_AUDIO_BIENVENIDA = "bienvenida.ogg"


def url_audio(archivo: str) -> str | None:
    base = url_base()
    return f"{base}/audios/{archivo}" if base else None


# --- Existencias -------------------------------------------------------------
# El catalogo trae "stock" (piezas) y "costo" por producto. Sin esto Rory prometia
# cosas que ya no habia (el Buzz del Duo, la Coleccion Completa sin Tiro al Blanco).

UMBRAL_ULTIMAS = 3   # de 1 a 3 piezas: se puede vender, pero hay que confirmar antes


def stock(producto_id: str) -> int:
    """Piezas fisicas en almacen."""
    p = PRODUCTOS.get((producto_id or "").strip())
    return int(p.get("stock", 0)) if p else 0


def comprometido(producto_id: str) -> int:
    """Piezas ya agendadas y no entregadas."""
    p = PRODUCTOS.get((producto_id or "").strip())
    return int(p.get("comprometido", 0)) if p else 0


def disponible(producto_id: str) -> int:
    """Lo que de verdad se puede vender hoy."""
    return max(0, stock(producto_id) - comprometido(producto_id))


def estado_stock(producto_id: str) -> str:
    """agotado | ultimas | ok"""
    n = disponible(producto_id)
    if n <= 0:
        return "agotado"
    return "ultimas" if n <= UMBRAL_ULTIMAS else "ok"


def estado_paquete(nombre_paquete: str) -> str:
    """Un paquete vale lo que su pieza mas escasa."""
    ids = PAQUETES_PUEBLA.get(nombre_paquete)
    if not ids:
        return "ok"
    estados = [estado_stock(i) for i in ids]
    if "agotado" in estados:
        return "agotado"
    return "ultimas" if "ultimas" in estados else "ok"


def texto_stock(modo: str = "") -> str:
    """
    Bloque para el system prompt. No repite el catalogo: solo lo que el modelo no
    puede saber solo. En Puebla se acota a Toy Story para no inflar el prompt.
    """
    def nombre(pid: str) -> str:
        return PRODUCTOS[pid]["nombre"]

    es_puebla = modo == "puebla"
    universo = [pid for pid, p in PRODUCTOS.items()
                if not es_puebla or p.get("categoria") == "Toy Story"]

    agotados = [nombre(i) for i in universo if estado_stock(i) == "agotado"]
    ultimas = [f"{nombre(i)} ({disponible(i)})" for i in universo if estado_stock(i) == "ultimas"]

    lineas = ["## Existencias (dato del sistema: manda sobre el catalogo de arriba)"]
    if agotados:
        lineas.append("AGOTADOS. No los ofrezcas, no los prometas y no los metas en ningun "
                      "paquete. Si te los piden, dilo de una vez y ofrece lo mas parecido "
                      "que si haya: " + "; ".join(sorted(agotados)) + ".")
    if ultimas:
        lineas.append(f"ULTIMAS PIEZAS (queda lo que dice el parentesis). Se venden, pero "
                      "nunca prometas varias ni apartes mas de las que quedan: "
                      + "; ".join(sorted(ultimas)) + ".")
    if es_puebla:
        bloqueados = [n for n in PAQUETES_PUEBLA if estado_paquete(n) == "agotado"]
        limitados = [n for n in PAQUETES_PUEBLA if estado_paquete(n) == "ultimas"]
        if bloqueados:
            lineas.append("PAQUETES QUE NO SE PUEDEN SURTIR: " + "; ".join(bloqueados)
                          + ". No los ofrezcas ni los menciones como opcion.")
        if limitados:
            lineas.append("PAQUETES AL LIMITE: " + "; ".join(limitados)
                          + ". Ofrecelos, pero di que quedan pocos y confirma antes de agendar.")
        lineas.append("De otras lineas (Barbie, Hot Wheels, Cars, preescolar, rompecabezas) "
                      "muchos modelos tienen de 1 a 3 piezas: antes de apartar o agendar uno, "
                      "confirma con avisar_equipo.")
    lineas.append("Nunca inventes existencias. Ante la duda, avisar_equipo en vez de prometer.")
    return "\n".join(lineas)
