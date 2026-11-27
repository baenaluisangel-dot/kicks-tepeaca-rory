# agent/voz.py — "Voz del cliente": convierte los chats en ideas para anuncios, videos y productos

"""
Tres pasos:
1. Etiquetar: Claude lee cada conversacion que cambio y anota que producto pidio, que dudo,
   que objecion tuvo y como termino. Se guarda en la tabla `etiquetas`.
2. Contar: con la base de datos se sacan los numeros exactos (sin inventar nada).
3. Sintetizar: Claude recibe esos numeros y frases reales y propone ideas de copy, videos,
   productos y mejoras de venta. Se guarda como un `Informe`.

Todo corre en segundo plano; el panel solo lo muestra. No manda nada a clientes.
"""

import asyncio
import csv
import io
import json
import logging
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select

from agent.brain import MODELO, client
from agent.catalogo import PAQUETES_PUEBLA
from agent.memory import (
    Entrega,
    Etiqueta,
    Informe,
    Mensaje,
    _sin_acentos,
    ahora,
    async_session,
    listar_avisos,
    metricas,
    transcripcion,
)

logger = logging.getLogger("agentkit")

# Leer cada conversacion es una tarea sencilla y repetida: se usa el modelo mas economico.
# Rory sigue contestando a los clientes con MODELO.
MODELO_CLASIFICAR = os.getenv("ANTHROPIC_MODEL_CLASIFICAR") or "claude-haiku-5-5"

# Categorias fijas: asi se pueden contar. Lo que no encaje cae en "otro".
DUDAS = ["medidas", "voz_o_sonidos", "pilas", "originalidad", "precio", "costo_envio",
         "zonas_o_horarios", "forma_de_pago", "fotos_o_videos", "disponibilidad", "apartado", "otro"]
OBJECIONES = ["precio_alto", "zona_no_cubierta", "horario_no_le_sirve", "desconfianza",
              "lo_va_a_pensar", "sin_motivo_claro", "ninguna"]
RESULTADOS = ["compro_o_agendo", "en_curso", "se_enfrio", "solo_curioseo", "queja"]
EN_ZONA = ["si", "no", "no_dijo"]

NOMBRES_DUDA = {
    "medidas": "¿Cuánto mide?", "voz_o_sonidos": "¿Habla / hace sonidos?", "pilas": "¿Trae pilas?",
    "originalidad": "¿Es original?", "precio": "Preguntas de precio", "costo_envio": "Costo de envío",
    "zonas_o_horarios": "Zonas u horarios", "forma_de_pago": "Forma de pago",
    "fotos_o_videos": "Pide fotos o videos", "disponibilidad": "¿Hay disponibles?",
    "apartado": "Apartado", "otro": "Otras dudas",
}
NOMBRES_OBJECION = {
    "precio_alto": "Le pareció caro", "zona_no_cubierta": "Su zona no está en la ruta",
    "horario_no_le_sirve": "El horario no le sirve", "desconfianza": "Desconfianza",
    "lo_va_a_pensar": "Dijo que lo pensaría", "sin_motivo_claro": "Se enfrió sin decir por qué",
    "ninguna": "Sin objeción",
}
NOMBRES_RESULTADO = {
    "compro_o_agendo": "Compró o agendó", "en_curso": "En curso", "se_enfrio": "Se enfrió",
    "solo_curioseo": "Solo curioseó", "queja": "Queja",
}

HERRAMIENTA_ETIQUETA = {
    "name": "registrar_etiqueta",
    "description": "Registra lo que se entiende de una conversacion entre un cliente y Rory.",
    "input_schema": {
        "type": "object",
        "properties": {
            "productos": {"type": "array", "items": {"type": "string"},
                          "description": "Juguetes o paquetes por los que pregunto, con el nombre del catalogo (ej: 'Buzz con alas', 'Los Héroes'). Vacio si no pregunto por ninguno."},
            "dudas": {"type": "array", "items": {"type": "string", "enum": DUDAS},
                      "description": "Categorias de las dudas que el cliente expreso."},
            "objecion": {"type": "string", "enum": OBJECIONES,
                         "description": "La razon principal por la que no avanzo o dudo. 'ninguna' si avanzo sin problema."},
            "resultado": {"type": "string", "enum": RESULTADOS},
            "frases": {"type": "array", "items": {"type": "string"},
                       "description": "Hasta 3 frases TEXTUALES y utiles del cliente (preguntas, dudas, como pide las cosas). Sin nombres propios, telefonos ni direcciones."},
            "zona": {"type": "string",
                     "description": "Zona, colonia o municipio donde el cliente dijo estar o querer recibir. Solo el nombre del lugar, sin calle ni numero. Vacio si no lo dijo."},
            "en_zona": {"type": "string", "enum": EN_ZONA,
                        "description": "'si': su lugar o el punto que acepto esta cubierto (WhatsApp: entrega en Amozoc, Valsequillo, CU, Peri Plaza, Angelopolis, Centro o CAPU, o cualquier punto de la ciudad de Puebla; Instagram y Facebook: puede ir a la tienda en Tepeaca). 'no': esta fuera de cobertura, pide envio a otra ciudad o ningun punto le sirve. 'no_dijo': no hay forma de saberlo."},
            "producto_elegido": {"type": "string",
                                 "description": "El juguete o paquete que el cliente dijo querer o escogio. Vacio si solo pidio informacion o precios sin inclinarse por ninguno."},
        },
        "required": ["productos", "dudas", "objecion", "resultado", "frases", "zona", "en_zona", "producto_elegido"],
    },
}

PROMPT_ETIQUETA = (
    "Eres analista de ventas de Kicks Tepeaca, una tienda de juguetes (Toy Story y otros) que vende por "
    "WhatsApp, Instagram y Facebook. Lees una conversacion entre un cliente y Rory (el asistente) y la "
    "registras con la herramienta. Se fiel a lo que el cliente dijo: no inventes dudas que no expreso. "
    "'compro_o_agendo' solo si quedo un pedido o una entrega acordada. 'se_enfrio' si el cliente dejo de "
    "contestar despues de recibir informacion. 'solo_curioseo' si pregunto por informacion general y no mostro "
    "intencion de compra."
)

HERRAMIENTA_SINTESIS = {
    "name": "registrar_informe",
    "description": "Entrega las recomendaciones del periodo.",
    "input_schema": {
        "type": "object",
        "properties": {
            "resumen": {"type": "string", "description": "2 o 3 frases con lo mas importante del periodo."},
            "ideas_copy": {"type": "array", "maxItems": 5, "items": {"type": "object", "properties": {
                "idea": {"type": "string", "description": "Un copy o gancho listo para usar en un anuncio."},
                "evidencia": {"type": "string", "description": "El dato o la frase de los chats que lo respalda."}},
                "required": ["idea", "evidencia"]}},
            "videos": {"type": "array", "maxItems": 5, "items": {"type": "object", "properties": {
                "idea": {"type": "string", "description": "Un video concreto por grabar."},
                "evidencia": {"type": "string"}}, "required": ["idea", "evidencia"]}},
            "productos": {"type": "array", "maxItems": 5, "items": {"type": "object", "properties": {
                "idea": {"type": "string", "description": "Que producto probar, destacar o dejar de empujar."},
                "evidencia": {"type": "string"}}, "required": ["idea", "evidencia"]}},
            "ventas": {"type": "array", "maxItems": 5, "items": {"type": "object", "properties": {
                "idea": {"type": "string", "description": "Un cambio concreto en como vende Rory o en la ruta."},
                "evidencia": {"type": "string"}}, "required": ["idea", "evidencia"]}},
        },
        "required": ["resumen", "ideas_copy", "videos", "productos", "ventas"],
    },
}

PROMPT_SINTESIS = (
    "Eres estratega de marketing de Kicks Tepeaca (juguetes, Toy Story; venta por WhatsApp en Puebla con entrega "
    "en zonas y por Instagram/Facebook en Tepeaca). Recibes numeros reales de las conversaciones del periodo y "
    "frases textuales de clientes. Propone ideas para: copies de anuncios, videos por grabar, productos por "
    "probar o destacar, y mejoras de venta. Reglas: usa SOLO lo que muestran los datos; cada idea cita su "
    "evidencia con cifras; si algo tiene pocos casos, dilo; no inventes numeros; ideas concretas y cortas, "
    "en espanol de Mexico, sin promesas de descuentos."
)

# Estado visible para el panel
estado = {"activo": False, "etapa": "", "hechas": 0, "total": 0, "error": "", "fin": None}

MIN_REPOSO_MIN = int(os.getenv("VOZ_REPOSO_MIN") or "45")     # espera a que el chat se calme
MAX_POR_CORRIDA = int(os.getenv("VOZ_MAX_CHATS") or "80")     # tope de chats por corrida (costo)


def _canal(tel: str) -> str:
    return "instagram" if tel.startswith("ig:") else "facebook" if tel.startswith("fb:") else "whatsapp"


def _aware(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


async def _pedir_registro(sistema: str, contenido: str, herramienta: dict, max_tokens: int,
                          modelo: str = MODELO) -> dict:
    """
    Pide a Claude que conteste con la herramienta y devuelve sus datos.

    El modelo no acepta que se le fuerce una herramienta (tool_choice "tool"), asi que se le
    pide en las instrucciones. Si contesta con puro texto, se intenta una vez mas.
    """
    orden = f"\n\nContesta unicamente llamando a la herramienta {herramienta['name']}, sin texto."
    for _ in range(2):
        resp = await client.messages.create(
            model=modelo, max_tokens=max_tokens, system=sistema + orden,
            messages=[{"role": "user", "content": contenido}], tools=[herramienta],
        )
        datos = next((b.input for b in resp.content if b.type == "tool_use"), None)
        if datos:
            return datos
    raise RuntimeError("Claude no entrego el registro")


# ── 1. Etiquetar ─────────────────────────────────────────────────────────────

async def _chats_por_etiquetar() -> list[tuple[str, int]]:
    """
    Conversaciones nuevas o que cambiaron desde la ultima etiqueta, ya calmadas.

    Solo las de clientes que siguieron escribiendo despues de la primera respuesta (o que
    agendaron): las demas no se leen, para no gastar en ellas.
    """
    dueno = (os.getenv("OWNER_WHATSAPP") or "").strip()
    async with async_session() as session:
        filas = (await session.execute(
            select(Mensaje.telefono, func.max(Mensaje.id), func.max(Mensaje.timestamp),
                   func.sum(case((Mensaje.role == "user", 1), else_=0))).group_by(Mensaje.telefono)
        )).all()
        # Las etiquetas viejas sin zona (en_zona vacio) se vuelven a leer una vez
        etiquetadas = dict((await session.execute(
            select(Etiqueta.telefono, Etiqueta.ultimo_msg_id).where(Etiqueta.en_zona != ""))).all())
        primera = (select(Mensaje.telefono.label("tel"), func.min(Mensaje.id).label("id"))
                   .where(Mensaje.role != "user").group_by(Mensaje.telefono).subquery())
        siguieron = set((await session.execute(
            select(Mensaje.telefono).join(primera, primera.c.tel == Mensaje.telefono)
            .where(Mensaje.role == "user", Mensaje.id > primera.c.id).distinct())).scalars().all())
        siguieron |= set((await session.execute(select(Entrega.telefono))).scalars().all())
    limite = ahora() - timedelta(minutes=MIN_REPOSO_MIN)
    pendientes = []
    for tel, ultimo_id, ultima_hora, n_cliente in filas:
        if dueno and tel == dueno:
            continue
        if not n_cliente or tel not in siguieron or _aware(ultima_hora) > limite:
            continue
        if ultimo_id > etiquetadas.get(tel, 0):
            pendientes.append((tel, ultimo_id))
    pendientes.sort(key=lambda x: x[1])
    return pendientes[:MAX_POR_CORRIDA]


def _texto_chat(turnos: list[dict]) -> str:
    lineas = []
    for t in turnos:
        if not t["texto"]:
            continue
        quien = "Cliente" if t["role"] == "user" else "Rory"
        lineas.append(f"{quien}: {t['texto']}")
    return "\n".join(lineas)[-8000:]


def zona_mx():
    return timezone(timedelta(hours=-6))


async def _etiquetar_uno(tel: str, ultimo_id: int):
    turnos = await transcripcion(tel, limite=80)
    texto = _texto_chat(turnos)
    if not texto.strip():
        return
    datos = await _pedir_registro(PROMPT_ETIQUETA, f"Canal: {_canal(tel)}\n\n{texto}", HERRAMIENTA_ETIQUETA, 4096,
                                  modelo=MODELO_CLASIFICAR)
    async with async_session() as session:
        agendo = (await session.execute(select(Entrega.id).where(Entrega.telefono == tel).limit(1))).first()
        resultado = "compro_o_agendo" if agendo else datos.get("resultado", "en_curso")
        if resultado not in RESULTADOS:
            resultado = "en_curso"
        dudas = [d for d in datos.get("dudas", []) if d in DUDAS]
        objecion = datos.get("objecion", "ninguna")
        fila = await session.get(Etiqueta, tel)
        if fila is None:
            fila = Etiqueta(telefono=tel)
            session.add(fila)
        fila.canal = _canal(tel)
        fila.productos = json.dumps([_dato(p)[:60] for p in datos.get("productos", []) if _dato(p)][:6], ensure_ascii=False)
        fila.dudas = json.dumps(dudas, ensure_ascii=False)
        fila.objecion = objecion if objecion in OBJECIONES else "sin_motivo_claro"
        fila.resultado = resultado
        fila.frases = json.dumps([_limpiar_frase(f) for f in datos.get("frases", [])][:3], ensure_ascii=False)
        fila.ultimo_msg_id = ultimo_id
        fila.zona = _limpiar_frase(_dato(datos.get("zona")))[:60]
        en_zona = datos.get("en_zona", "no_dijo")
        fila.en_zona = en_zona if en_zona in EN_ZONA else "no_dijo"
        fila.producto_elegido = _limpiar_frase(_dato(datos.get("producto_elegido")))[:80]
        # La fecha de la etiqueta es la de la conversacion, no la de la lectura: asi
        # "ultimos 7 dias" no incluye chats viejos que se acaban de releer
        fila.actualizado_en = turnos[-1]["hora"]
        await session.commit()


_VACIOS = {"", "ninguno", "ninguna", "vacio", "vacío", "n/a", "na", "null", "none", "no dijo", "no_dijo"}


def _dato(valor) -> str:
    """El modelo economico a veces escribe comillas vacias o "ninguno" en lugar de dejar el campo vacio."""
    texto = str(valor or "").strip().strip("\"'").strip()
    return "" if texto.lower() in _VACIOS else texto


def _limpiar_frase(f: str) -> str:
    """Quita telefonos y recorta: los informes no deben llevar datos personales."""
    f = re.sub(r"\+?\d[\d\s\-]{7,}\d", "[número]", str(f))
    return f.strip()[:160]


async def etiquetar_pendientes() -> int:
    pendientes = await _chats_por_etiquetar()
    estado.update(etapa="Leyendo conversaciones", hechas=0, total=len(pendientes))
    candado = asyncio.Semaphore(3)
    errores = 0

    async def uno(tel, ultimo_id):
        nonlocal errores
        async with candado:
            try:
                await _etiquetar_uno(tel, ultimo_id)
            except Exception as e:  # noqa: BLE001
                errores += 1
                logger.error(f"No se pudo etiquetar una conversacion: {type(e).__name__}: {e}")
            estado["hechas"] += 1

    await asyncio.gather(*(uno(t, i) for t, i in pendientes))
    if pendientes and errores == len(pendientes):
        raise RuntimeError("Claude no pudo analizar ninguna conversacion (revisa la API key o el saldo)")
    return len(pendientes) - errores


# ── 2. Contar ────────────────────────────────────────────────────────────────

def _top(contador: Counter, n: int = 8) -> list[list]:
    return [[k, v] for k, v in contador.most_common(n)]


async def datos_periodo(dias: int) -> dict:
    desde = ahora() - timedelta(days=dias)
    m = await metricas(desde)
    async with async_session() as session:
        etiquetas = (await session.execute(select(Etiqueta).where(Etiqueta.actualizado_en >= desde))).scalars().all()
        entregas = (await session.execute(select(Entrega).where(Entrega.creado_en >= desde))).scalars().all()
    productos, dudas, objeciones, resultados = Counter(), Counter(), Counter(), Counter()
    por_canal: dict[str, Counter] = {}
    frases: list[str] = []
    for e in etiquetas:
        productos.update(json.loads(e.productos or "[]"))
        dudas.update(json.loads(e.dudas or "[]"))
        if e.objecion != "ninguna":
            objeciones[e.objecion] += 1
        resultados[e.resultado] += 1
        por_canal.setdefault(e.canal, Counter())[e.resultado] += 1
        frases.extend(json.loads(e.frases or "[]"))
    zonas = Counter(x.zona for x in entregas if x.zona and x.estado != "cancelada")
    paquetes = Counter()
    for x in entregas:
        if x.estado == "cancelada":
            continue
        pedido = _sin_acentos(x.pedido or "")
        for nombre in PAQUETES_PUEBLA:
            if _sin_acentos(nombre) in pedido:
                paquetes[nombre] += 1
    avisos = await listar_avisos(desde)
    sin_catalogo = [a["motivo"] for a in avisos if a["tipo"] == "fuera_de_catalogo"][:30]
    dudas_producto = [a["motivo"] for a in avisos if a["tipo"] == "duda_producto"][:30]
    vistas = set()
    frases_unicas = []
    for f in frases:
        clave = f.lower()
        if f and clave not in vistas:
            vistas.add(clave)
            frases_unicas.append(f)
    return {
        "dias": dias,
        "conversaciones_analizadas": len(etiquetas),
        "por_canal_nuevas": m["por_canal"],
        "resultados": _top(resultados, 10),
        "resultados_por_canal": {c: dict(v) for c, v in por_canal.items()},
        "productos_mas_pedidos": _top(productos, 10),
        "dudas": _top(dudas, 12),
        "objeciones": _top(objeciones, 8),
        "entregas_agendadas": len([x for x in entregas if x.estado != "cancelada"]),
        "piezas_sueltas": len([x for x in entregas if x.suelto and x.estado != "cancelada"]),
        "entregas_por_zona": _top(zonas, 8),
        "paquetes_agendados": _top(paquetes, 9),
        "pidieron_algo_que_no_tenemos": sin_catalogo,
        "dudas_que_rory_paso_al_equipo": dudas_producto,
        "frases_de_clientes": frases_unicas[:40],
    }


# ── 3. Sintetizar ────────────────────────────────────────────────────────────

async def generar_informe(dias: int = 7) -> dict:
    estado.update(etapa="Armando recomendaciones")
    datos = await datos_periodo(dias)
    muestra_chica = datos["conversaciones_analizadas"] < 30
    sintesis = {}
    if datos["conversaciones_analizadas"] > 0:
        sintesis = await _pedir_registro(PROMPT_SINTESIS, json.dumps(datos, ensure_ascii=False),
                                         HERRAMIENTA_SINTESIS, 8000)
    contenido = {"metricas": datos, "sintesis": sintesis, "muestra_chica": muestra_chica}
    async with async_session() as session:
        fila = Informe(dias=dias, datos=json.dumps(contenido, ensure_ascii=False), creado_en=ahora())
        session.add(fila)
        await session.commit()
    return contenido


async def ultimo_informe() -> tuple[datetime, dict] | None:
    async with async_session() as session:
        fila = (await session.execute(select(Informe).order_by(Informe.id.desc()).limit(1))).scalar_one_or_none()
    if not fila:
        return None
    return fila.creado_en, json.loads(fila.datos or "{}")


async def analizar(dias: int = 7) -> dict | None:
    """Corre todo el proceso. Si ya hay una corrida en marcha, no empieza otra."""
    if estado["activo"]:
        return None
    estado.update(activo=True, etapa="Empezando", hechas=0, total=0, error="")
    try:
        await etiquetar_pendientes()
        return await generar_informe(dias)
    except Exception as e:  # noqa: BLE001
        logger.exception(f"Fallo el analisis de voz del cliente: {e}")
        estado["error"] = f"{type(e).__name__}: {e}"[:300]
        return None
    finally:
        estado.update(activo=False, etapa="", fin=ahora())


async def clasificar_pendientes() -> int:
    """Lee todas las conversaciones pendientes, por tandas. No genera informe."""
    if estado["activo"]:
        return 0
    estado.update(activo=True, etapa="Empezando", hechas=0, total=0, error="")
    total = 0
    try:
        for _ in range(40):
            hechas = await etiquetar_pendientes()
            total += hechas
            if hechas < MAX_POR_CORRIDA:
                break
    except Exception as e:  # noqa: BLE001
        logger.error(f"No se pudieron clasificar las conversaciones: {type(e).__name__}: {e}")
        estado["error"] = f"{type(e).__name__}: {e}"[:300]
    finally:
        estado.update(activo=False, etapa="", fin=ahora())
    return total


async def bucle_clasificar():
    """Al arrancar se pone al dia y despues clasifica una vez al dia, a HORA_HOJA (hora de Mexico)."""
    hora = int(os.getenv("HORA_HOJA") or "22")
    await asyncio.sleep(120)
    while True:
        n = await clasificar_pendientes()
        if n:
            logger.info(f"Conversaciones clasificadas para la hoja: {n}")
        ahora_mx = datetime.now(zona_mx())
        objetivo = ahora_mx.replace(hour=hora, minute=0, second=0, microsecond=0)
        if objetivo <= ahora_mx:
            objetivo += timedelta(days=1)
        await asyncio.sleep((objetivo - ahora_mx).total_seconds())


def texto_telegram(contenido: dict) -> str:
    m, s = contenido.get("metricas", {}), contenido.get("sintesis", {})
    lineas = [f"🧠 Voz del cliente — últimos {m.get('dias', 7)} días",
              f"Conversaciones analizadas: {m.get('conversaciones_analizadas', 0)}"]
    if contenido.get("muestra_chica"):
        lineas.append("(muestra chica: tómalo como pista, no como regla)")
    if s.get("resumen"):
        lineas += ["", s["resumen"]]
    for titulo, clave in (("🎯 Copies", "ideas_copy"), ("🎬 Videos", "videos"), ("🧸 Productos", "productos"), ("💬 Ventas", "ventas")):
        for it in (s.get(clave) or [])[:2]:
            lineas.append(f"{titulo}: {it['idea']}")
    lineas += ["", "El informe completo está en el panel, en “Voz del cliente”."]
    return "\n".join(lineas)


async def bucle_voz_semanal():
    """Cada domingo a HORA_VOZ (hora de Mexico) analiza la semana y avisa por Telegram."""
    from agent.notificar import avisar_por_telegram
    hora = int(os.getenv("HORA_VOZ") or "20")
    zona = zona_mx()
    while True:
        ahora_mx = datetime.now(zona)
        objetivo = ahora_mx.replace(hour=hora, minute=0, second=0, microsecond=0)
        objetivo += timedelta(days=(6 - ahora_mx.weekday()) % 7)
        if objetivo <= ahora_mx:
            objetivo += timedelta(days=7)
        await asyncio.sleep((objetivo - ahora_mx).total_seconds())
        try:
            contenido = await analizar(7)
            if contenido:
                await avisar_por_telegram(texto_telegram(contenido))
        except Exception as e:  # noqa: BLE001
            logger.error(f"No se pudo mandar el informe semanal: {e}")


# ── Exportar ─────────────────────────────────────────────────────────────────

async def exportar_csv() -> str:
    """Las etiquetas en CSV, sin telefonos: solo un numero de fila por conversacion."""
    async with async_session() as session:
        filas = (await session.execute(select(Etiqueta).order_by(Etiqueta.actualizado_en))).scalars().all()
    salida = io.StringIO()
    w = csv.writer(salida)
    w.writerow(["n", "canal", "fecha", "productos", "dudas", "objecion", "resultado", "frases"])
    for i, e in enumerate(filas, 1):
        w.writerow([i, e.canal, e.actualizado_en.strftime("%Y-%m-%d"),
                    " | ".join(json.loads(e.productos or "[]")), " | ".join(json.loads(e.dudas or "[]")),
                    e.objecion, e.resultado, " | ".join(json.loads(e.frases or "[]"))])
    return salida.getvalue()
