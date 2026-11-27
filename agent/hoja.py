# agent/hoja.py — Los datos para la hoja de Google Sheets del equipo de campañas

"""
Dos tablas en CSV que Google Sheets lee sola con IMPORTDATA:

- Calificadas: una fila por conversacion que eligio producto (y no esta fuera de zona),
  agendo o compro.
- Resumen diario: un conteo por dia y anuncio.

Las conversaciones donde el cliente no escribio nada despues de la primera respuesta no se
registran (ni se leen con Claude), salvo que hayan agendado o comprado.

No salen nombres. El telefono sale solo en "Calificadas" (por decision del dueño, para
ubicar al cliente y anotar su venta en el panel). Cada conversacion lleva ademas un codigo
anonimo y estable. La direccion se protege con una clave propia, de solo lectura, distinta a la del panel.
"""

import csv
import hashlib
import hmac
import io
import json
import os
import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from sqlalchemy import select

from agent.memory import Entrega, Etiqueta, Mensaje, Origen, Venta, async_session
from agent.voz import _aware, _canal, zona_mx

router = APIRouter()

COLUMNAS_CALIFICADAS = [
    "fecha", "conversacion", "telefono", "canal", "ad_id", "ad_name", "campaign_name", "zona", "en_zona",
    "producto_elegido", "agendo", "fecha_entrega", "zona_entrega", "venta_mxn", "resultado",
    "objecion", "dudas", "frases", "ultimo_mensaje",
]
ESTADOS = ["calificadas", "fuera_zona", "sin_interes", "otro", "sin_clasificar"]
COLUMNAS_RESUMEN = ["fecha", "canal", "ad_id", "ad_name", "campaign_name", "convos_totales", *ESTADOS,
                    "agendadas", "ventas", "venta_mxn"]


def clave_hoja() -> str:
    """Clave de solo lectura, derivada de PANEL_TOKEN: no sirve para entrar al panel."""
    base = os.getenv("PANEL_TOKEN", "")
    if not base:
        return ""
    return hmac.new(base.encode(), b"hoja-v1", hashlib.sha256).hexdigest()[:40]


def _codigo(telefono: str) -> str:
    return "C-" + hmac.new(clave_hoja().encode(), telefono.encode(), hashlib.sha256).hexdigest()[:8]


def _telefono(telefono: str) -> str:
    """El numero como se marca en Mexico (10 digitos). Instagram y Facebook no traen numero."""
    if not telefono.isdigit():
        return ""
    for prefijo, largo in (("521", 13), ("52", 12)):
        if telefono.startswith(prefijo) and len(telefono) == largo:
            return telefono[len(prefijo):]
    return telefono


def _campo(datos: str, clave: str) -> str:
    """Lee un dato del anuncio guardado. El JSON puede venir recortado, por eso no se usa json.loads."""
    m = re.search(rf'"{clave}"\s*:\s*"((?:[^"\\]|\\.)*)"', datos or "")
    if not m:
        return ""
    try:
        return json.loads(f'"{m.group(1)}"')
    except ValueError:
        return m.group(1)


def clasificar(*, agendo: bool, compro: bool, hubo_respuesta: bool, cliente_siguio: bool, etiqueta) -> str:
    """En cual de los grupos cae una conversacion. El orden importa."""
    if agendo or compro:
        return "calificadas"
    if not hubo_respuesta:
        return "sin_clasificar"          # Rory todavia no le contesta
    if not cliente_siguio:
        return "no_contesto"             # no escribio nada despues de la primera respuesta
    if etiqueta is None or not etiqueta.en_zona:
        return "sin_clasificar"          # Claude todavia no la lee
    if etiqueta.en_zona == "no":
        return "fuera_zona"
    if etiqueta.producto_elegido:
        return "calificadas"
    if etiqueta.resultado == "queja":
        return "otro"
    return "sin_interes"


async def conversaciones() -> list[dict]:
    """Una entrada por conversacion, con su anuncio, su grupo y lo que paso."""
    dueno = (os.getenv("OWNER_WHATSAPP") or "").strip()
    async with async_session() as session:
        mensajes = (await session.execute(
            select(Mensaje.telefono, Mensaje.role, Mensaje.timestamp).order_by(Mensaje.id))).all()
        origenes = (await session.execute(select(Origen).order_by(Origen.id))).scalars().all()
        etiquetas = {e.telefono: e for e in (await session.execute(select(Etiqueta))).scalars().all()}
        entregas = (await session.execute(
            select(Entrega).where(Entrega.estado != "cancelada").order_by(Entrega.id))).scalars().all()
        ventas = (await session.execute(select(Venta))).scalars().all()

    chats: dict[str, dict] = {}
    for tel, rol, hora in mensajes:
        c = chats.setdefault(tel, {"inicio": hora, "ultimo": hora, "cliente": 0, "hubo_respuesta": False,
                                   "cliente_siguio": False})
        c["ultimo"] = hora
        if rol == "user":
            c["cliente"] += 1
            if c["hubo_respuesta"]:
                c["cliente_siguio"] = True
        else:
            c["hubo_respuesta"] = True

    primer_origen, agenda, cobrado = {}, {}, {}
    for o in origenes:
        primer_origen.setdefault(o.telefono, o)
    for e in entregas:
        agenda[e.telefono] = e           # queda la mas reciente
    for v in ventas:
        n, total = cobrado.get(v.telefono, (0, 0))
        cobrado[v.telefono] = (n + 1, total + v.monto)

    salida = []
    for tel, c in chats.items():
        if tel == dueno or not c["cliente"]:
            continue
        o, et, en = primer_origen.get(tel), etiquetas.get(tel), agenda.get(tel)
        n_ventas, monto = cobrado.get(tel, (0, 0))
        grupo = clasificar(agendo=en is not None, compro=n_ventas > 0, hubo_respuesta=c["hubo_respuesta"],
                           cliente_siguio=c["cliente_siguio"], etiqueta=et)
        if grupo == "no_contesto":
            continue
        salida.append({
            "fecha": _aware(c["inicio"]).astimezone(zona_mx()).strftime("%Y-%m-%d"),
            "ultimo_mensaje": _aware(c["ultimo"]).astimezone(zona_mx()).strftime("%Y-%m-%d %H:%M"),
            "conversacion": _codigo(tel),
            "telefono": _telefono(tel),
            "canal": _canal(tel),
            # Con prefijo: como numero, Sheets redondea los ID de 18 digitos y ya no cruzan con Meta
            "ad_id": f"ad:{o.anuncio_id}" if o and o.anuncio_id else "sin_dato",
            "ad_name": (_campo(o.datos, "name") or o.titulo) if o else "",
            "campaign_name": _campo(o.datos, "campaignName") if o else "",
            "grupo": grupo,
            "zona": et.zona if et else "",
            "en_zona": et.en_zona if et else "",
            "producto_elegido": (et.producto_elegido if et else "") or (en.pedido if en else ""),
            "agendo": "si" if en else "no",
            "fecha_entrega": en.fecha if en else "",
            "zona_entrega": en.zona if en else "",
            "ventas": n_ventas,
            "venta_mxn": monto,
            # "compro_o_agendo" solo si hay entrega o venta registrada; si no, es intencion
            "resultado": ("compro_o_agendo" if en or n_ventas else "" if not et
                          else "intencion_de_compra" if et.resultado == "compro_o_agendo" else et.resultado),
            "objecion": et.objecion if et else "",
            "dudas": " | ".join(json.loads(et.dudas or "[]")) if et else "",
            "frases": " | ".join(json.loads(et.frases or "[]")) if et else "",
        })
    return salida


def _celda(valor):
    """Un texto que empiece como formula no debe ejecutarse en la hoja."""
    if isinstance(valor, str):
        valor = " ".join(valor.split())
        if valor[:1] in ("=", "+", "-", "@"):
            return "'" + valor
    return valor


def _csv(columnas: list[str], filas: list[dict]) -> str:
    salida = io.StringIO()
    w = csv.writer(salida)
    w.writerow(columnas)
    for f in filas:
        w.writerow([_celda(f.get(c, "")) for c in columnas])
    return salida.getvalue()


async def csv_calificadas() -> str:
    filas = [c for c in await conversaciones() if c["grupo"] == "calificadas"]
    filas.sort(key=lambda c: c["ultimo_mensaje"], reverse=True)
    return _csv(COLUMNAS_CALIFICADAS, filas)


async def csv_resumen() -> str:
    grupos: dict[tuple, dict] = {}
    for c in await conversaciones():
        clave = (c["fecha"], c["canal"], c["ad_id"])
        g = grupos.get(clave)
        if g is None:
            g = grupos[clave] = {"fecha": c["fecha"], "canal": c["canal"], "ad_id": c["ad_id"],
                                 "ad_name": "", "campaign_name": "", "convos_totales": 0,
                                 **{e: 0 for e in ESTADOS}, "agendadas": 0, "ventas": 0, "venta_mxn": 0}
        g["ad_name"] = g["ad_name"] or c["ad_name"]
        g["campaign_name"] = g["campaign_name"] or c["campaign_name"]
        g["convos_totales"] += 1
        g[c["grupo"]] += 1
        g["agendadas"] += c["agendo"] == "si"
        g["ventas"] += c["ventas"]
        g["venta_mxn"] += c["venta_mxn"]
    filas = sorted(grupos.values(), key=lambda g: (g["fecha"], g["convos_totales"]), reverse=True)
    return _csv(COLUMNAS_RESUMEN, filas)


def _exigir_clave(clave: str):
    esperada = clave_hoja()
    if not esperada or not hmac.compare_digest((clave or "").encode(), esperada.encode()):
        raise HTTPException(status_code=404)


@router.get("/hoja/calificadas.csv")
async def hoja_calificadas(clave: str = ""):
    _exigir_clave(clave)
    return Response(await csv_calificadas(), media_type="text/csv; charset=utf-8")


@router.get("/hoja/resumen.csv")
async def hoja_resumen(clave: str = ""):
    _exigir_clave(clave)
    return Response(await csv_resumen(), media_type="text/csv; charset=utf-8")
