# agent/interactivo.py — Listas de opciones de WhatsApp (zonas y dias de entrega)

"""
Arma las listas interactivas (el menu que se abre con un boton) y traduce lo que el cliente
elige a texto para que Rory lo lea como si lo hubiera escrito.

Cada opcion lleva un id con prefijo:  zona:<Zona>   |   dia:<YYYY-MM-DD>|<Zona>
"""

from datetime import date

from agent.catalogo import ZONAS_PUEBLA

DIAS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
DIAS_CORTO = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
MESES_ES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
            "septiembre", "octubre", "noviembre", "diciembre"]
MESES_CORTO = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]

# Limites de WhatsApp para listas
MAX_FILAS = 10
MAX_TITULO = 24
MAX_DESCRIPCION = 72
MAX_BOTON = 20
MAX_CUERPO = 1024


def _fecha_larga(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{DIAS_ES[d.weekday()]} {d.day} de {MESES_ES[d.month - 1]}"


def _fecha_corta(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{DIAS_CORTO[d.weekday()]} {d.day} {MESES_CORTO[d.month - 1]}"


def lista_zonas() -> dict:
    filas = [{"id": f"zona:{z}", "title": z[:MAX_TITULO], "description": "Entrega a domicilio"[:MAX_DESCRIPCION]} for z in ZONAS_PUEBLA]
    filas.append({"id": "zona:otra", "title": "Otra zona", "description": "El equipo confirma si llegamos"})
    texto = "¿En cuál zona te queda mejor la entrega?\n" + "\n".join(f"• {z}" for z in ZONAS_PUEBLA) + "\n• Otra zona"
    return {
        "cuerpo": "¿En cuál zona te queda mejor la entrega? Toca el botón y elige 👇",
        "boton": "Ver zonas",
        "filas": filas[:MAX_FILAS],
        "texto_alterno": texto,
        "nota": "lista de zonas de entrega",
    }


def lista_dias(zona: str, dias: list[dict]) -> dict:
    """dias: [{"fecha": "YYYY-MM-DD", "horario": "...", "quedan": n}]"""
    filas = []
    for d in dias[: MAX_FILAS - 1]:
        desc = f"{d['horario'] or 'Horario por confirmar'} · quedan {d['quedan']}"
        filas.append({"id": f"dia:{d['fecha']}|{zona}", "title": _fecha_corta(d["fecha"])[:MAX_TITULO],
                      "description": desc[:MAX_DESCRIPCION]})
    filas.append({"id": f"dia:otro|{zona}", "title": "Otro día", "description": "El equipo te confirma"})
    texto = f"Días de entrega en {zona}:\n" + "\n".join(
        f"• {_fecha_larga(d['fecha'])} ({d['horario'] or 'horario por confirmar'})" for d in dias[: MAX_FILAS - 1]
    ) + "\n¿Cuál te queda mejor?"
    nota = f"lista de días de entrega para {zona}: " + ", ".join(f"{d['fecha']} ({_fecha_corta(d['fecha'])})" for d in dias[: MAX_FILAS - 1])
    return {
        "cuerpo": f"Estos son los días de entrega en {zona}. Toca el botón y elige el que te acomode 👇",
        "boton": "Ver días",
        "filas": filas,
        "texto_alterno": texto,
        "nota": nota,
    }


def texto_de_seleccion(opcion_id: str) -> str | None:
    """Convierte el id de la opcion que toco el cliente en una frase para Rory."""
    if not opcion_id:
        return None
    if opcion_id.startswith("zona:"):
        z = opcion_id[5:]
        return "Mi zona es otra, no aparece en la lista" if z == "otra" else f"Mi zona es {z}"
    if opcion_id.startswith("dia:"):
        resto = opcion_id[4:]
        fecha, _, zona = resto.partition("|")
        if fecha == "otro":
            return "Prefiero otro día, ninguno de la lista me acomoda"
        try:
            return f"Elijo el {_fecha_larga(fecha)} ({fecha}) para la entrega en {zona}"
        except ValueError:
            return None
    return None
