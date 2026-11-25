# agent/calendario.py — La fecha de hoy y los dias de entrega, para que Rory no adivine

from datetime import datetime, timedelta, timezone

from agent.memory import dias_disponibles, obtener_config_ruta

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
         "septiembre", "octubre", "noviembre", "diciembre"]
DIAS_A_MOSTRAR = 8


def ahora_mx() -> datetime:
    """Hora de Mexico (UTC-6 fijo, sin horario de verano desde 2022)."""
    return datetime.now(timezone(timedelta(hours=-6)))


def nombre_dia(d) -> str:
    return f"{DIAS[d.weekday()]} {d.day} de {MESES[d.month - 1]}"


async def texto_calendario(modo: str) -> str:
    ahora = ahora_mx()
    hoy = ahora.date()
    manana = hoy + timedelta(days=1)
    lineas = [
        "## Fecha y hora (dato del sistema)",
        f"Ahora es {nombre_dia(hoy)} de {hoy.year}, {ahora.strftime('%H:%M')} (hora de México).",
        f"HOY es {nombre_dia(hoy)} ({hoy.isoformat()}). MAÑANA es {nombre_dia(manana)} ({manana.isoformat()}).",
        "Ya sabes qué día es: nunca lo adivines ni se lo preguntes al cliente. Si en un mensaje "
        "anterior alguien dijo \"mañana\", puede referirse a otro día: revísalo antes de contestar.",
    ]
    if modo != "puebla":
        return "\n".join(lineas)

    cfg = await obtener_config_ruta()
    por_fecha: dict[str, list[str]] = {}
    for p in cfg["paradas"]:
        for d in await dias_disponibles(p["zona"], hoy.isoformat()):
            por_fecha.setdefault(d["fecha"], []).append(f"{p['zona']} ({d['horario'] or 'horario por confirmar'})")
    # Zonas que solo estan en dias extra abiertos a mano ya quedan cubiertas: todas son paradas
    lineas.append("")
    lineas.append("## Entregas que el sistema permite agendar en Puebla (manda sobre cualquier otro dato)")
    for n in range(DIAS_A_MOSTRAR):
        d = hoy + timedelta(days=n)
        etiqueta = ("HOY " if n == 0 else "MAÑANA " if n == 1 else "") + nombre_dia(d)
        zonas = por_fecha.get(d.isoformat())
        lineas.append(f"- {etiqueta}: " + (", ".join(zonas) if zonas else "NO hay entregas que puedas agendar"))
    puntos = "; ".join(f"{p['zona']}: {p['punto']}" for p in cfg["paradas"] if p.get("punto"))
    if puntos:
        lineas.append(f"Puntos de entrega por zona: {puntos}.")
    return "\n".join(lineas)
