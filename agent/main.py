# agent/main.py — Servidor FastAPI + Webhook de WhatsApp
# Generado por AgentKit

"""
Servidor principal del agente.
Funciona con cualquier proveedor (Zernio, Meta) gracias a la capa de providers.
"""

import asyncio
import logging
import html
import random
from datetime import timedelta
import re
import time
import os
from collections import defaultdict
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from agent.brain import MAX_FOTOS_POR_RESPUESTA, generar_respuesta, obtener_mensaje_error
from agent.catalogo import (
    ARCHIVO_CATALOGO_PDF,
    ARCHIVO_AUDIO_BIENVENIDA,
    ARCHIVO_PAQUETES_PDF,
    url_audio,
    PAQUETES_PUEBLA,
    VIDEOS_TOY_STORY,
    ZONAS_PUEBLA,
    url_video,
    url_paquetes_pdf,
    obtener_producto,
    pie_de_foto,
    url_catalogo_pdf,
    url_foto,
)
from agent.memory import (
    guardar_mensaje,
    guardar_origen,
    agenda_reciente,
    borrar_pendientes,
    cargar_pendientes,
    esta_pausada,
    guardar_pendiente,
    listar_atencion,
    marcar_necesita_persona,
    necesita_persona,
    borrar_ejemplo,
    guardar_contexto_dueno,
    obtener_contexto_dueno,
    borrar_ruta,
    guardar_ejemplo,
    listar_ejemplos,
    cambiar_estado_entrega,
    crear_entrega,
    crear_ruta,
    dias_disponibles,
    punto_de_entrega,
    rutas_con_pedido_grande,
    listar_avisos,
    listar_entregas,
    listar_rutas,
    metricas,
    registrar_aviso,
    cancelar_envios_de,
    registrar_mensaje_cliente,
    transcripcion,
    ultimo_aviso_por_telefono,
    guardar_modo,
    obtener_modo,
    pausar,
    reanudar,
    inicializar_db,
    liberar_evento,
    limpiar_eventos_viejos,
    marcar_evento_procesado,
    obtener_historial,
)
from agent.memory import (  # cola de aprobaciones y bitacora
    crear_espera,
    guardar_mensaje_telegram,
    listar_esperas,
    registrar_bitacora,
    resolver_espera,
)
from agent import calendario, interactivo
from agent.audio import transcribir
from agent.notificar import (
    avisar_con_botones,
    avisar_por_telegram,
    chat_autorizado,
    confirmar_toque,
    editar_aviso,
    enlace_cliente,
    registrar_webhook,
)
from agent.despachador import bucle_despachador
from agent.hoja import router as hoja_router
from agent.mercado import router as mercado_router
from agent.voz import bucle_clasificar, bucle_voz_semanal
from agent.panel import bucle_resumen_diario, fecha_bonita, fecha_hoy_iso, router as panel_router
from agent.providers import obtener_proveedor
from agent.providers.base import MensajeEntrante

load_dotenv()

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("agentkit")
# En desarrollo queremos el detalle de NUESTRO agente, no el de las librerias.
# Poner el nivel raiz en DEBUG llena la terminal de ruido de aiosqlite y httpx
# y hace imposible leer lo que hizo el agente.
logger.setLevel(logging.DEBUG if ENVIRONMENT == "development" else logging.INFO)

# httpx registra la URL completa de cada peticion a nivel INFO, y Telegram mete el
# token del bot DENTRO de la ruta:
#   POST https://api.telegram.org/bot<TOKEN>/sendMessage
# Eso dejaba el token en texto plano en los logs de Railway, en cada aviso. Con el
# bot aprobando decisiones de negocio, ese token vale mas que ayer.
# A WARNING se siguen viendo los fallos de red, que es lo que importa diagnosticar.
for _ruidosa in ("httpx", "httpcore"):
    logging.getLogger(_ruidosa).setLevel(logging.WARNING)

PORT = int(os.getenv("PORT", "8000"))

# Un candado por numero de telefono. En WhatsApp es normal que alguien mande "hola" y
# medio segundo despues la pregunta de verdad: sin esto los dos mensajes se procesarian
# en paralelo, los dos leerian el mismo historial y las escrituras quedarian intercaladas.
_candados: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# Si la configuracion esta mal, guardamos el error y lo mostramos en el health check,
# en vez de reventar en el import y dejar a Railway reiniciando el contenedor a ciegas.
proveedor = None
error_configuracion: str | None = None
try:
    proveedor = obtener_proveedor()
except Exception as e:  # noqa: BLE001 — cualquier problema de configuracion
    error_configuracion = str(e)

# Resultado del chequeo de credenciales que se hace al arrancar. Se expone en el health
# check: que el servidor conteste no significa que el agente pueda responder por WhatsApp.
estado_proveedor: dict = {"ok": None, "detalle": "sin verificar"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Prepara la base de datos y chequea el proveedor al arrancar."""
    await inicializar_db()
    await limpiar_eventos_viejos()
    logger.info("Base de datos lista")
    await recuperar_pendientes()
    asyncio.create_task(bucle_resumen_diario())
    asyncio.create_task(bucle_voz_semanal())
    asyncio.create_task(bucle_clasificar())
    asyncio.create_task(bucle_despachador(lambda: proveedor))
    logger.info(f"Servidor AgentKit escuchando en el puerto {PORT}")

    global estado_proveedor
    if proveedor is not None:
        logger.info(f"Proveedor de WhatsApp: {proveedor.__class__.__name__}")
        ok, detalle = await proveedor.verificar_conexion()
        estado_proveedor = {"ok": ok, "detalle": detalle}
        logger.info(f"Conexion con el proveedor: {'OK' if ok else 'ERROR'} — {detalle}")
    else:
        logger.error(f"Proveedor de WhatsApp NO configurado: {error_configuracion}")

    # Los botones de Telegram no sirven de nada si Telegram no sabe a donde mandar el
    # toque. Se registra en cada arranque: setWebhook es idempotente, y si falla el
    # agente arranca igual, solo que sin poder recibir aprobaciones.
    ok_tg, detalle_tg = await registrar_webhook()
    if ok_tg:
        logger.info(f"Webhook de Telegram registrado: {detalle_tg}")
    else:
        logger.warning(f"Webhook de Telegram NO registrado: {detalle_tg}")

    yield


_ultimos_textos: dict[tuple[str, str], float] = {}
_ultimo_recordatorio: dict[str, float] = {}

app = FastAPI(title="AgentKit — WhatsApp AI Agent", version="2.0.0", lifespan=lifespan)
app.include_router(panel_router)
app.include_router(hoja_router)
app.include_router(mercado_router)

# Fotos de los productos. WhatsApp las descarga de aqui, asi que tienen que ser publicas.
app.mount("/fotos", StaticFiles(directory="static/fotos"), name="fotos")
# Catalogo de juguetes en PDF (se manda como documento cuando lo piden).
app.mount("/catalogo", StaticFiles(directory="static/catalogo"), name="catalogo")
# Videos cortos de los juguetes (MP4).
app.mount("/videos", StaticFiles(directory="static/videos"), name="videos")
# Nota de voz de bienvenida (OGG/Opus).
app.mount("/audios", StaticFiles(directory="static/audios"), name="audios")


@app.get("/")
async def health_check():
    """Endpoint de salud para Railway y monitoreo."""
    if error_configuracion:
        return {"status": "error", "service": "agentkit", "detalle": error_configuracion}

    # Se responde 200 aunque las credenciales esten mal, para que Railway no marque el
    # deploy como caido y puedas leer el diagnostico. El detalle esta en el cuerpo.
    return {
        "status": "ok" if estado_proveedor["ok"] else "degradado",
        "service": "agentkit",
        "proveedor": proveedor.__class__.__name__ if proveedor else None,
        "conexion": estado_proveedor,
    }


@app.get("/webhook")
async def webhook_verificacion(request: Request):
    """Verificacion GET del webhook. La pide Meta; para Zernio no hace nada."""
    if proveedor is None:
        raise HTTPException(status_code=503, detail=error_configuracion or "Proveedor no configurado")

    respuesta = await proveedor.validar_webhook(request)
    if respuesta is not None:
        return PlainTextResponse(respuesta)

    # Meta pide un 403 cuando manda hub.mode=subscribe y el verify_token no coincide.
    # Devolverle 200 le hace creer que la URL quedo verificada cuando no es cierto.
    if request.query_params.get("hub.mode") == "subscribe":
        raise HTTPException(status_code=403, detail="Verify token incorrecto")

    return {"status": "ok"}


@app.post("/webhook")
async def webhook_handler(request: Request, tareas: BackgroundTasks):
    """
    Recibe los mensajes de WhatsApp.

    Contesta 200 de inmediato y procesa el mensaje en segundo plano.

    Esto NO es un detalle de estilo. Los proveedores esperan un 2xx en unos 5 segundos y,
    si no lo reciben, reintentan el mismo evento hasta 7 veces. Como llamar a Claude tarda
    mas que eso, procesar antes de contestar hace que el cliente reciba la misma respuesta
    repetida. Por eso: responder primero, trabajar despues.
    """
    if proveedor is None:
        raise HTTPException(status_code=503, detail=error_configuracion or "Proveedor no configurado")

    if not await proveedor.verificar_firma(request):
        raise HTTPException(status_code=401, detail="Firma del webhook invalida")

    try:
        mensajes = await proveedor.parsear_webhook(request)
    except Exception as e:  # noqa: BLE001
        # Un payload raro no debe hacer que el proveedor reintente para siempre
        logger.error(f"No se pudo leer el webhook: {e}")
        return {"status": "ignorado"}

    encolados = 0
    for msg in mensajes:
        if msg.es_propio:
            await _atender_mensaje_del_equipo(msg)
            continue
        if msg.contexto.get("anuncio"):
            # Se guarda antes de filtrar: a veces los datos llegan en un mensaje sin texto.
            # Si falla, Rory contesta igual.
            try:
                await guardar_origen(msg.telefono, msg.contexto.get("plataforma", "whatsapp"),
                                     msg.contexto["anuncio"])
            except Exception as e:  # noqa: BLE001
                logger.error(f"No se pudo guardar el anuncio de origen: {e}")
        await _recordar_chat_del_dueno(msg)
        es_audio = bool(msg.contexto.get("audio_url")) and not msg.texto.strip()
        if not msg.texto.strip() and not es_audio:
            continue

        # La entrega es "al menos una vez": el mismo evento puede llegar dos veces
        evento_id = msg.contexto.get("evento_id") or msg.mensaje_id
        if evento_id and not await marcar_evento_procesado(evento_id):
            logger.info(f"Evento repetido, se ignora: {evento_id}")
            continue

        if es_audio:
            # Hay que bajar el audio YA (Meta lo borra despues de un rato) y transcribirlo:
            # se hace en segundo plano para contestar el webhook a tiempo.
            logger.info(f"Nota de voz de {msg.telefono}: se transcribe")
            asyncio.create_task(_transcribir_y_aceptar(msg))
            continue

        if await _aceptar_mensaje(msg):
            encolados += 1

    return {"status": "ok", "encolados": encolados}


@app.post("/telegram")
async def telegram_webhook(request: Request):
    """
    Recibe el toque de un boton de Telegram y resuelve la espera correspondiente.

    Dos barreras, porque este endpoint aprueba decisiones de negocio:
      1. El secreto del webhook, que solo conocen Telegram y nosotros.
      2. El chat: solo el de Luisito decide. Cualquier otro se descarta.
    """
    secreto = (os.getenv("TELEGRAM_WEBHOOK_SECRET") or "").strip()
    if secreto and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secreto:
        logger.warning("Webhook de Telegram con secreto invalido: rechazado")
        raise HTTPException(status_code=401, detail="Secreto invalido")

    try:
        cuerpo = await request.json()
    except Exception:  # noqa: BLE001 — un payload roto no debe reintentarse para siempre
        return {"status": "ignorado"}

    toque = cuerpo.get("callback_query")
    if not toque:
        return {"status": "ignorado"}  # mensajes normales al bot: no hacemos nada

    chat = ((toque.get("message") or {}).get("chat") or {}).get("id")
    if not chat_autorizado(chat):
        logger.warning(f"Toque de Telegram desde un chat no autorizado: {chat}")
        await confirmar_toque(toque.get("id", ""), "No autorizado")
        return {"status": "no autorizado"}

    dato = toque.get("data") or ""
    if not dato.startswith("e:"):
        await confirmar_toque(toque.get("id", ""))
        return {"status": "ignorado"}

    # callback_data llega como "e:<id>:<clave>"
    partes = dato[2:].split(":", 1)
    try:
        espera_id = int(partes[0])
    except (ValueError, IndexError):
        await confirmar_toque(toque.get("id", ""))
        return {"status": "ignorado"}
    clave = partes[1] if len(partes) > 1 else ""

    fila = await resolver_espera(espera_id, clave)
    if fila is None:
        # Ya la habia contestado: dos toques seguidos no la resuelven dos veces
        await confirmar_toque(toque.get("id", ""), "Ya estaba contestada")
        return {"status": "ya resuelta"}

    await confirmar_toque(toque.get("id", ""), "Listo")
    mensaje = (toque.get("message") or {}).get("message_id") or fila.get("mensaje_telegram")
    if mensaje:
        await editar_aviso(int(mensaje), f"{fila['asunto']}\n\nRespondiste: {clave}")
    await registrar_bitacora(fila["agente"], f"espera {espera_id} resuelta",
                             f"Luisito respondio: {clave}", "ok")
    logger.info(f"Espera {espera_id} resuelta por Telegram: {clave}")
    return {"status": "ok", "espera": espera_id, "respuesta": clave}


async def pedir_aprobacion(
    agente: str,
    asunto: str,
    detalle: str = "",
    opciones: list[dict] | None = None,
    costo_esperar: int = 0,
    telefono: str = "",
) -> int:
    """
    Encola una decision para Luisito y se la manda por Telegram con botones.
    Es la unica via por la que un agente pide permiso: asi nada queda solo en un
    aviso que se pierde en el scroll.
    """
    opciones = opciones or [{"clave": "si", "etiqueta": "Sí"}, {"clave": "no", "etiqueta": "No"}]
    espera_id = await crear_espera(agente, asunto, detalle, opciones, costo_esperar, telefono)

    lineas = [f"{agente} necesita tu visto bueno", "", asunto]
    if detalle:
        lineas += ["", detalle]
    if costo_esperar:
        lineas += ["", f"Cuesta esperar: ${costo_esperar:,}"]
    if telefono:
        lineas += ["", enlace_cliente(telefono, "whatsapp")]

    # La clave del boton viaja con el id de la espera, para no depender de cual fue la ultima
    botones = [{"clave": f"{espera_id}:{o['clave']}", "etiqueta": o["etiqueta"]} for o in opciones]
    mensaje_id = await avisar_con_botones("\n".join(lineas), botones)
    if mensaje_id:
        await guardar_mensaje_telegram(espera_id, mensaje_id)
    else:
        logger.warning(f"Espera {espera_id} quedo encolada pero no se pudo avisar por Telegram")
    return espera_id


async def avisar_dueno(texto: str):
    """Avisa al dueño por WhatsApp si su chat esta abierto (ventana de 24 h); si no, por Telegram."""
    tel = (os.getenv("OWNER_WHATSAPP") or "").strip().lstrip("+")
    if tel and proveedor is not None:
        try:
            ctx = await obtener_contexto_dueno()
            if ctx and await proveedor.enviar_mensaje(tel, texto, ctx):
                return
        except Exception as e:  # noqa: BLE001
            logger.warning(f"No se pudo avisar al dueño por WhatsApp: {e}")
    await avisar_por_telegram(texto)


async def _recordar_chat_del_dueno(msg: MensajeEntrante):
    """Cuando el dueño escribe desde su numero, guarda su chat para poder avisarle por WhatsApp
    (WhatsApp solo deja escribir libremente dentro de las 24 h del ultimo mensaje del usuario)."""
    tel = (os.getenv("OWNER_WHATSAPP") or "").strip().lstrip("+")
    if tel and msg.telefono == tel and msg.contexto.get("conversation_id"):
        await guardar_contexto_dueno({k: v for k, v in msg.contexto.items()
                                      if k in ("conversation_id", "account_id", "plataforma", "cuenta_zernio")})


async def _transcribir_y_aceptar(msg: MensajeEntrante):
    """Baja la nota de voz, la transcribe y sigue el camino normal de un mensaje de texto."""
    try:
        datos = await proveedor.descargar_adjunto(msg.contexto["audio_url"], msg.contexto)
        texto = await transcribir(datos) if datos else None
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error transcribiendo la nota de voz de {msg.telefono}: {e}")
        texto = None
    if texto:
        logger.info(f"Nota de voz de {msg.telefono} transcrita ({len(texto)} letras)")
        msg.texto = f"[Nota de voz del cliente, transcrita]: {texto}"
    else:
        msg.texto = "[El cliente mandó un archivo, foto o nota de voz que no se puede leer]"
    await _aceptar_mensaje(msg)


async def _aceptar_mensaje(msg: MensajeEntrante) -> bool:
    """Deduplica por texto, registra el mensaje y lo encola (o avisa si Rory esta en pausa)."""
    # Instagram a veces entrega el mismo mensaje dos veces con ids distintos (sobre
    # todo el predeterminado de los anuncios). Mismo cliente + mismo texto en menos
    # de 60 s = duplicado, no se contesta dos veces.
    ahora = time.monotonic()
    clave = (msg.telefono, msg.texto.strip().lower())
    if ahora - _ultimos_textos.get(clave, -1e9) < 60:
        logger.info(f"Mensaje duplicado de {msg.telefono}, se ignora")
        return False
    _ultimos_textos[clave] = ahora
    if len(_ultimos_textos) > 2000:
        for k in [k for k, t in _ultimos_textos.items() if ahora - t > 60]:
            _ultimos_textos.pop(k, None)

    await registrar_mensaje_cliente(
        msg.telefono, msg.contexto.get("plataforma", "whatsapp"), msg.texto, msg.contexto
    )
    # El cliente contesto: lo que quedaba programado para insistirle ya no tiene
    # sentido, y mandarlo encima de su mensaje queda muy mal.
    cancelados = await cancelar_envios_de(msg.telefono)
    if cancelados:
        logger.info(f"{cancelados} envios cancelados: {msg.telefono} contesto")
    if await esta_pausada(msg.telefono):
        logger.info(f"Mensaje de {msg.telefono} sin contestar: Rory esta en pausa ahi")
        # El cliente sigue escribiendo y esta esperando a una persona: recordatorio
        # (maximo uno cada 10 minutos por cliente).
        if await necesita_persona(msg.telefono) and ahora - _ultimo_recordatorio.get(msg.telefono, -1e9) > 600:
            _ultimo_recordatorio[msg.telefono] = ahora
            plat = msg.contexto.get("plataforma", "whatsapp")
            await avisar_dueno(
                f"💬 Un cliente que espera a una persona volvió a escribir.\n"
                f"Cliente: {enlace_cliente(msg.telefono, plat)}\nMensaje: {msg.texto[:300]}"
            )
        return False

    logger.info(f"Mensaje de {msg.telefono}: {msg.texto}")
    await _encolar(msg)
    return True


_RE_PAUSA = re.compile(
    r"rory.{0,25}(deja|dejes|pausa|pausar|detente|para|alto|apag|stop|no (contest|respond|opere))"
    r"|(deja|dejes|detente|apaga).{0,12}(de operar|de contestar|de responder).{0,25}(rory|bot)"
    r"|\b(bot|rory)\s*(off|stop)\b|yo me encargo|nosotros nos encargamos",
    re.I,
)
_RE_REANUDAR = re.compile(
    r"rory.{0,25}(act[ií]v|reanud|contin|vuelve|retoma|prende)|\b(bot|rory)\s*on\b", re.I
)
HORAS_PAUSA_HUMANO = 12
HORAS_PAUSA_AVISO = 24


async def _atender_mensaje_del_equipo(msg: MensajeEntrante):
    """El equipo escribio en el chat: comando de pausa/reanudar, o pausa automatica."""
    texto = msg.texto.strip()
    if _RE_REANUDAR.search(texto):
        await reanudar(msg.telefono)
        logger.info(f"{msg.telefono}: Rory reactivado por el equipo")
    elif texto and _RE_PAUSA.search(texto):
        await pausar(msg.telefono)
        logger.info(f"{msg.telefono}: Rory pausado por el equipo (hasta que lo reactiven)")
    elif msg.contexto.get("humano"):
        await marcar_necesita_persona(msg.telefono, False)
        await pausar(msg.telefono, HORAS_PAUSA_HUMANO)
        logger.info(f"{msg.telefono}: respondio una persona; Rory en pausa {HORAS_PAUSA_HUMANO} h")


# Rory no contesta al instante: espera unos minutos (aleatorio) para que no se sienta
# automatico, junta los mensajes que lleguen mientras tanto y, si en ese tiempo una persona
# del equipo entra a la conversacion, no contesta.
DELAY_MIN = float(os.getenv("RESPUESTA_DELAY_MIN") or "120")
DELAY_MAX = float(os.getenv("RESPUESTA_DELAY_MAX") or "240")
# Cuando ya se esta acordando la entrega, esperar tanto confunde: se contesta mas rapido.
DELAY_AGENDA_MIN = float(os.getenv("RESPUESTA_DELAY_AGENDA_MIN") or "30")
DELAY_AGENDA_MAX = float(os.getenv("RESPUESTA_DELAY_AGENDA_MAX") or "60")
_RE_AGENDA = re.compile(
    r"\b(hoy|mañana|manana|entreg\w*|horario|zona|ubicaci\w*|d[oó]nde|a qu[eé] hora|elijo el)\b", re.I
)
_pendientes: dict[str, list[MensajeEntrante]] = {}
_trabajadores: dict[str, asyncio.Task] = {}


_primer_pendiente: dict[str, "datetime"] = {}


_espera_recuperado: dict[str, tuple[float, float]] = {}


async def _encolar(msg: MensajeEntrante):
    from datetime import datetime, timezone
    # Se guarda en la base: si el servidor se reinicia (deploy) el mensaje no se pierde.
    try:
        msg.contexto["_pend_id"] = await guardar_pendiente(
            msg.telefono, msg.texto, msg.mensaje_id, msg.contexto
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"No se pudo guardar el mensaje pendiente: {e}")
    _primer_pendiente.setdefault(msg.telefono, datetime.now(timezone.utc))
    _pendientes.setdefault(msg.telefono, []).append(msg)
    if msg.telefono not in _trabajadores:
        _trabajadores[msg.telefono] = asyncio.create_task(_trabajador(msg.telefono))


async def recuperar_pendientes():
    """Al arrancar: retoma los mensajes que quedaron esperando cuando se reinicio el servidor."""
    filas = await cargar_pendientes()
    for f in filas:
        ctx = dict(f["contexto"])
        ctx["_pend_id"] = f["id"]
        msg = MensajeEntrante(
            telefono=f["telefono"], texto=f["texto"], mensaje_id=f["mensaje_id"],
            es_propio=False, contexto=ctx,
        )
        _primer_pendiente.setdefault(f["telefono"], f["creado_en"])
        _pendientes.setdefault(f["telefono"], []).append(msg)
        _espera_recuperado[f["telefono"]] = (10.0, 40.0)  # ya esperaron: contestar pronto
    for tel in list(_pendientes):
        if tel not in _trabajadores:
            _trabajadores[tel] = asyncio.create_task(_trabajador(tel))
    if filas:
        logger.info(f"Se recuperaron {len(filas)} mensajes pendientes tras el reinicio")


def _texto_del_lote(lote: list) -> str:
    """Junta los mensajes del cliente. Los '[Unsupported message]' (imagenes, audios, la
    tarjeta del anuncio) se descartan si hay texto real junto a ellos."""
    textos = [m.texto.strip() for m in lote]
    reales = [x for x in textos if x != "[Unsupported message]"]
    if reales:
        return "\n".join(reales)
    return "[El cliente mandó un archivo, foto o nota de voz que no se puede leer]"


async def _esperar_para_contestar(telefono: str, minimo: float, maximo: float):
    """Espera como una persona; si se esta hablando de la entrega, la espera se acorta."""
    reloj = asyncio.get_running_loop().time
    inicio = reloj()
    limite = inicio + random.uniform(minimo, maximo)
    corto = inicio + random.uniform(DELAY_AGENDA_MIN, max(DELAY_AGENDA_MIN, DELAY_AGENDA_MAX))
    try:
        en_agenda = await agenda_reciente(telefono)
    except Exception as e:  # noqa: BLE001
        logger.error(f"No se pudo revisar si habia agenda en curso: {e}")
        en_agenda = False
    while (resta := limite - reloj()) > 0:
        if corto < limite and (en_agenda or any(_RE_AGENDA.search(m.texto) for m in _pendientes.get(telefono, []))):
            # Al menos 10 s mas, por si el cliente sigue escribiendo
            limite = min(limite, max(corto, reloj() + 10))
            corto = limite
            continue
        await asyncio.sleep(min(5, resta))


async def _trabajador(telefono: str):
    try:
        while _pendientes.get(telefono):
            lo, hi = _espera_recuperado.pop(telefono, (DELAY_MIN, max(DELAY_MIN, DELAY_MAX)))
            await _esperar_para_contestar(telefono, lo, hi)
            lote = _pendientes.pop(telefono, [])
            desde = _primer_pendiente.pop(telefono, None)
            ids_lote = [m.contexto.get("_pend_id") for m in lote if m.contexto.get("_pend_id")]
            if not lote:
                break
            if await esta_pausada(telefono):
                logger.info(f"{telefono}: una persona tomo la conversacion; Rory no contesta")
                await borrar_pendientes(ids_lote)
                continue
            ultimo = lote[-1]
            # Respaldo: si una persona ya contesto en este chat, Rory se hace a un lado.
            consulta = getattr(proveedor, "hubo_respuesta_humana", None)
            if consulta and desde and await consulta(ultimo.contexto, desde):
                logger.info(f"{telefono}: una persona ya contesto; Rory se pausa y no contesta")
                await pausar(telefono, HORAS_PAUSA_HUMANO)
                await marcar_necesita_persona(telefono, False)
                await borrar_pendientes(ids_lote)
                continue
            unido = MensajeEntrante(
                telefono=telefono,
                texto=_texto_del_lote(lote),
                mensaje_id=ultimo.mensaje_id,
                es_propio=False,
                contexto=ultimo.contexto,
            )
            await procesar_mensaje(unido)
            await borrar_pendientes(ids_lote)
    except Exception as e:  # noqa: BLE001
        logger.exception(f"Error en el trabajador de {telefono}: {e}")
    finally:
        _trabajadores.pop(telefono, None)
        if _pendientes.get(telefono):  # llegaron mensajes justo al terminar
            _trabajadores[telefono] = asyncio.create_task(_trabajador(telefono))


def _es_campana_puebla(msg: MensajeEntrante) -> bool:
    """True si el mensaje viene de un anuncio de Puebla (por el anuncio o por el texto)."""
    pistas = f"{msg.texto} {msg.contexto.get('referral', '')}".lower()
    return "puebla" in pistas or "paquetes de toy story" in pistas


async def procesar_mensaje(msg: MensajeEntrante):
    """
    Genera la respuesta y la manda de vuelta. Corre fuera del ciclo del webhook.

    Se toma un candado por telefono: dos mensajes seguidos del mismo cliente se
    atienden en orden, no en paralelo, para que el historial no se mezcle.
    """
    evento_id = msg.contexto.get("evento_id") or msg.mensaje_id

    async with _candados[msg.telefono]:
        try:
            # El historial se lee ANTES de guardar el mensaje actual: brain.py agrega
            # el mensaje nuevo al final, y asi no queda duplicado.
            historial = await obtener_historial(msg.telefono)

            # Campana de origen: "puebla" si el mensaje o el anuncio lo dicen; se guarda para
            # que el resto de la conversacion siga en ese modo (los mensajes siguientes ya
            # no traen el dato del anuncio).
            # El canal decide la campana: WhatsApp = Puebla (paquetes, pago contra entrega);
            # Instagram y Facebook = Tepeaca (juguetes, apartado).
            plataforma = msg.contexto.get("plataforma", "whatsapp")
            modo = "puebla" if plataforma == "whatsapp" else "tepeaca"
            es_puebla = modo == "puebla"
            es_instagram = plataforma in ("instagram", "facebook")  # canales sin telefono
            fotos_mandadas: list[str] = []   # nombres de productos, para la nota del historial
            contador = {"fotos": 0}          # fotos enviadas en esta respuesta

            async def enviar_foto(producto_id: str, todas: bool = False) -> str:
                """La usa Claude (herramienta enviar_foto) para mandar fotos de un producto."""
                producto = obtener_producto(producto_id)
                if not producto:
                    return f"ERROR: no existe el producto '{producto_id}'."
                rutas = producto.get("fotos") or []
                if not rutas:
                    return "ERROR: ese producto no tiene fotos. Ofrece que el equipo se las manda."
                if not todas:
                    rutas = rutas[:1]
                rutas = rutas[: MAX_FOTOS_POR_RESPUESTA - contador["fotos"]]
                if not rutas:
                    return "ERROR: ya se mandaron suficientes fotos en este mensaje. No mandes mas."

                total = len(producto["fotos"]) if todas else 1
                enviadas = 0
                for n, ruta in enumerate(rutas, 1):
                    url = url_foto(ruta)
                    if not url:
                        return "ERROR: no hay URL publica configurada para las fotos."
                    ok = await proveedor.enviar_imagen(
                        msg.telefono, url, pie_de_foto(producto, n, total), msg.contexto
                    )
                    if not ok:
                        break
                    enviadas += 1
                    contador["fotos"] += 1
                if enviadas:
                    fotos_mandadas.append(
                        producto["nombre"] + (f" ({enviadas} fotos)" if enviadas > 1 else "")
                    )
                    return f"OK: {enviadas} foto(s) de {producto['nombre']} enviada(s)."
                return "ERROR: no se pudo enviar la foto. Ofrece que el equipo se la manda."

            async def enviar_fotos_paquete(paquete: str) -> str:
                """La usa Claude en Puebla: una foto de cada juguete que trae el paquete."""
                ids = PAQUETES_PUEBLA.get(paquete)
                if not ids:
                    return f"ERROR: no existe el paquete '{paquete}'."
                enviadas = 0
                for pid in ids:
                    producto = obtener_producto(pid)
                    rutas = (producto or {}).get("fotos") or []
                    url = url_foto(rutas[0]) if rutas else None
                    if not url:
                        continue
                    if await proveedor.enviar_imagen(msg.telefono, url, producto["nombre"], msg.contexto):
                        enviadas += 1
                if enviadas:
                    fotos_mandadas.append(f"fotos del paquete {paquete}")
                    return f"OK: {enviadas} foto(s) del paquete {paquete} enviada(s)."
                return "ERROR: no se pudieron enviar las fotos. Ofrece que el equipo se las manda."

            contador_videos = {"n": 0}

            async def enviar_video(video: str) -> str:
                """La usa Claude en Puebla: manda un video corto del juguete."""
                info = VIDEOS_TOY_STORY.get(video)
                if not info:
                    return f"ERROR: no hay video '{video}'. Avisa al equipo con avisar_equipo (duda_producto)."
                if contador_videos["n"] >= 3:
                    return "ERROR: ya se mandaron 3 videos en este mensaje. No mandes mas."
                url = url_video(info[0])
                if not url:
                    return "ERROR: no hay URL publica configurada para los videos."
                ok = await proveedor.enviar_video(msg.telefono, url, "", msg.contexto)
                if not ok:  # ultimo recurso: como archivo
                    ok = await proveedor.enviar_documento(msg.telefono, url, info[0], "", msg.contexto)
                if ok:
                    contador_videos["n"] += 1
                    fotos_mandadas.append(f"video de {video}")
                    return f"OK: video de {video} enviado."
                return "ERROR: no se pudo enviar el video. Ofrece que el equipo se lo manda."

            async def enviar_catalogo(tipo: str = "") -> str:
                """La usa Claude (herramienta enviar_catalogo) para mandar el PDF del catalogo."""
                paquetes = es_puebla and tipo != "juguetes"
                url = url_paquetes_pdf() if paquetes else url_catalogo_pdf()
                archivo = ARCHIVO_PAQUETES_PDF if paquetes else ARCHIVO_CATALOGO_PDF
                if not url:
                    return "ERROR: no hay URL publica configurada para el catalogo."
                # Se manda el PDF como archivo (en Instagram tambien). Si Instagram lo
                # rechaza, se cae al enlace como ultimo recurso.
                # En Instagram el pie de foto sale como un globo aparte (ruido): sin texto.
                pie = "Catálogo de paquetes Toy Story 🎁" if paquetes else "Catálogo de juguetes de Kicks 🎁"
                ok = await proveedor.enviar_documento(
                    msg.telefono,
                    url,
                    archivo,
                    "" if es_instagram else pie,
                    msg.contexto,
                )
                if not ok and es_instagram:
                    ok = await proveedor.enviar_documento(
                        msg.telefono, url, archivo, pie, msg.contexto
                    )
                if not ok and es_instagram:
                    ok = await proveedor.enviar_mensaje(
                        msg.telefono,
                        f"{pie}\n{url}",
                        msg.contexto,
                    )
                if ok:
                    fotos_mandadas.append("el catálogo de paquetes en PDF" if paquetes else "el catálogo de juguetes en PDF")
                    return "OK: catalogo en PDF enviado."
                return "ERROR: no se pudo enviar el PDF. Ofrece que el equipo se lo manda."

            # En Puebla el PDF de paquetes se manda SIEMPRE en el primer turno, sin depender
            # de que el modelo decida usar la herramienta.
            texto_para_ia = msg.texto
            # Un numero con historial de Tepeaca (apartado, juguetes sueltos) contaminaria las
            # respuestas: en Puebla solo se usa el historial desde que se mando el PDF de paquetes.
            if es_puebla:
                inicio = next(
                    (i for i, m in enumerate(historial) if "paquetes en PDF" in m["content"]), None
                )
                historial = [] if inicio is None else historial[max(inicio - 1, 0):]
            ya_mando_pdf = any("paquetes en PDF" in m["content"] for m in historial)
            if es_puebla and not ya_mando_pdf:
                if (await enviar_catalogo()).startswith("OK"):
                    # Nota de voz de bienvenida (grabada por el dueño): da confianza
                    url_nota = url_audio(ARCHIVO_AUDIO_BIENVENIDA)
                    if url_nota and await proveedor.enviar_audio(msg.telefono, url_nota, msg.contexto):
                        fotos_mandadas.append("la nota de voz de bienvenida")
                    texto_para_ia += (
                        "\n\n[Nota interna: el sistema ya le envió el PDF de paquetes en este "
                        "turno; no uses enviar_catalogo.]"
                    )

            async def avisar_equipo(motivo: str, tipo: str = "otro") -> str:
                """La usa Claude. Dudas y cosas fuera de catalogo solo se anotan; pedidos, quejas y
                casos que necesitan una persona avisan al dueño (y las quejas pausan a Rory)."""
                await registrar_aviso(msg.telefono, tipo, motivo)
                silencioso = tipo in ("duda_producto", "fuera_de_catalogo")
                if tipo == "fuera_de_catalogo":
                    logger.info(f"{msg.telefono}: anotado ({tipo}) — {motivo}")
                    return ("OK: anotado. Dile al cliente de una vez que ese producto no lo manejas (sin decir que lo "
                            "vas a consultar) y ofrecele lo mas parecido del catalogo.")
                if silencioso:
                    logger.info(f"{msg.telefono}: anotado ({tipo}) — {motivo}")
                    return ("OK: anotado en el panel. No se pausa la conversacion. Dile al cliente en una frase "
                            "que lo confirmas mas tarde con el equipo y sigue ayudandolo con lo demas."
                            " IMPORTANTE: lo que escribiste antes de usar esta herramienta NO le llega al cliente: tu mensaje final debe "
                            "contestar primero todo lo que si sabes.")
                await marcar_necesita_persona(msg.telefono, True, motivo)
                pausa = tipo in ("queja_o_problema", "otro")
                if pausa:
                    await pausar(msg.telefono, HORAS_PAUSA_AVISO)
                logger.info(f"{msg.telefono}: necesita atencion ({tipo}) — {motivo}")
                icono = {"pedido": "🟢 PEDIDO POR CONFIRMAR", "queja_o_problema": "🔴 Necesito que atiendas a un cliente"
                         }.get(tipo, "🔵 Un cliente necesita a una persona")
                if motivo.startswith("URGENTE"):
                    icono = "🚨🚨 URGENTE: contesta ya, el cliente espera respuesta"
                await avisar_dueno(
                    f"{icono}\nCliente: {enlace_cliente(msg.telefono, plataforma)}\n"
                    f"Motivo: {motivo}\nÚltimo mensaje: {msg.texto[:300]}"
                )
                if pausa:
                    return "OK: el equipo quedo avisado. Dile al cliente que un companero le contesta por este chat."
                return "OK: el equipo quedo avisado del pedido. Sigue atendiendo al cliente."

            async def ver_dias_entrega(zona: str) -> str:
                """La usa Claude: dias de entrega abiertos para la zona del cliente."""
                dias = await dias_disponibles(zona, fecha_hoy_iso())
                if not dias:
                    return ("No hay dias de entrega abiertos para esa zona por ahora. Usa avisar_equipo "
                            "con tipo 'pedido' y dile al cliente que el equipo le confirma el dia.")
                lineas = [
                    f"- {d['fecha']} ({fecha_bonita(d['fecha'])}), horario {d['horario'] or 'por confirmar'}, "
                    f"quedan {d['quedan']} lugares"
                    for d in dias[:5]
                ]
                return "Dias disponibles en " + zona + ":\n" + "\n".join(lineas)

            async def agendar_entrega(fecha: str, zona: str, nombre: str, pedido: str, preferencia_horario: str = "", pieza_suelta: bool = False) -> str:
                """La usa Claude: agenda la entrega (por confirmar) y avisa al equipo."""
                if preferencia_horario.strip():
                    pedido = f"{pedido} — Horario preferido: {preferencia_horario.strip()}"
                ok, detalle = await crear_entrega(msg.telefono, fecha, zona, nombre, pedido, fecha_hoy_iso(), pieza_suelta)
                if pieza_suelta and ok:
                    pedido = f"{pedido} [PIEZA SUELTA: se lleva aprovechando la ruta]"
                if not ok:
                    return f"ERROR: {detalle} Vuelve a llamar ver_dias_entrega y ofrece otro dia."
                await marcar_necesita_persona(msg.telefono, True, f"Entrega {fecha} {zona}: {pedido}")
                await registrar_aviso(msg.telefono, "pedido", f"Entrega agendada {fecha} en {zona} — {nombre}: {pedido}")
                logger.info(f"{msg.telefono}: entrega agendada {fecha} {zona}")
                await avisar_dueno(
                    f"🟢 ENTREGA AGENDADA (por confirmar existencia)\n"
                    f"{fecha_bonita(fecha)} · {zona} · {detalle or ''}\n"
                    f"Cliente: {nombre} — {enlace_cliente(msg.telefono, plataforma)}\nPedido: {pedido}"
                )
                punto = await punto_de_entrega(zona)
                if pieza_suelta:
                    donde = (f" Como es una pieza suelta, sin costo si el cliente se acerca al punto de entrega: {punto} (a domicilio en la ciudad de Puebla son $150 adicionales)."
                             if punto else " Como es una pieza suelta, sin costo si el cliente se acerca a la zona (el equipo le confirma el punto); a domicilio en la ciudad de Puebla son $150 adicionales.")
                else:
                    donde = (f" El punto de entrega de la zona es: {punto} (si pidio que se la lleven a su lugar, anotaste su referencia)."
                             if punto else "")
                return (f"OK: agendada para {fecha_bonita(fecha)} en {zona}"
                        f"{', horario ' + detalle if detalle else ''}.{donde} Dile al cliente que quedo agendada "
                        "y que un companero le confirma la existencia por este chat.")

            lista_pendiente: dict = {}

            async def lista_zonas_tool() -> str:
                """La usa Claude: programa la lista de zonas (se manda despues de su texto)."""
                lista_pendiente["datos"] = interactivo.lista_zonas()
                return "OK: se enviara la lista de zonas al cliente. Si hay preguntas sin contestar, contestalas todas en tu texto; si no, escribe una frase muy corta o nada."

            async def lista_dias_tool(zona: str) -> str:
                """La usa Claude: programa la lista de dias de entrega abiertos para la zona."""
                dias = await dias_disponibles(zona, fecha_hoy_iso())
                if not dias:
                    return ("No hay dias de entrega abiertos para esa zona por ahora. Usa avisar_equipo "
                            "con tipo 'pedido' y dile al cliente que el equipo le confirma el dia.")
                lista_pendiente["datos"] = interactivo.lista_dias(zona, dias)
                manana = (calendario.ahora_mx().date() + timedelta(days=1))
                aviso = ("" if any(d["fecha"] == manana.isoformat() for d in dias) else
                         f" OJO: MAÑANA ({calendario.nombre_dia(manana)}) NO hay entrega en {zona}; si el cliente "
                         "pidio mañana, dilo claro en tu texto.")
                return ("OK: se enviara la lista de dias al cliente. Si hay preguntas sin contestar, contestalas "
                        "todas en tu texto; si no, escribe una frase muy corta o nada." + aviso)

            async def lista_rutas_tool() -> str:
                """La usa Claude para una pieza suelta: dias y zonas donde ya vamos a pasar."""
                items = await rutas_con_pedido_grande(fecha_hoy_iso())
                if not items:
                    return ("No hay ninguna ruta con pedido grande agendado en los proximos dias. Dile al cliente que "
                            "una pieza suelta solo se entrega cuando ya vamos a su zona con otro pedido y ofrecele "
                            "agregar otro juguete (con 2 o mas se agenda normal) o que el equipo le avisa cuando haya ruta.")
                lista_pendiente["datos"] = interactivo.lista_rutas(items)
                return "OK: se enviara la lista de rutas al cliente. Si hay preguntas sin contestar, contestalas todas en tu texto; si no, escribe una frase muy corta o nada."

            respuesta, es_respuesta_real = await generar_respuesta(
                texto_para_ia,
                historial,
                enviar_foto,
                enviar_catalogo,
                plataforma,
                modo,
                avisar_equipo,
                enviar_fotos_paquete if es_puebla else None,
                None,
                agendar_entrega if es_puebla else None,
                (await listar_ejemplos(modo, 15))[::-1],
                enviar_video if es_puebla else None,
                lista_zonas_tool if es_puebla else None,
                lista_dias_tool if es_puebla else None,
                lista_rutas_tool if es_puebla else None,
            )

            enviado = await proveedor.enviar_mensaje(msg.telefono, respuesta, msg.contexto) if respuesta else True

            # Lista de opciones (zonas o dias): va despues del texto. Si Zernio la rechaza,
            # se manda como texto con las opciones.
            datos_lista = lista_pendiente.get("datos")
            if enviado and datos_lista:
                if not await proveedor.enviar_lista(
                    msg.telefono, datos_lista["cuerpo"], datos_lista["boton"], datos_lista["filas"], msg.contexto
                ):
                    await proveedor.enviar_mensaje(msg.telefono, datos_lista["texto_alterno"], msg.contexto)
                fotos_mandadas.append(datos_lista["nota"])

            if not enviado:
                # El evento se marco como procesado ANTES de llegar hasta aca, para que dos
                # entregas simultaneas no se dupliquen. Si el envio fallo, hay que soltarlo:
                # si no, el reintento del proveedor se descartaria por duplicado y el cliente
                # se quedaria sin respuesta para siempre.
                logger.error(f"No se pudo enviar la respuesta a {msg.telefono}; se libera el evento")
                await liberar_evento(evento_id)
                return

            # Solo se guarda en el historial lo que de verdad es conversacion. Los avisos
            # tecnicos ("estoy teniendo problemas") no son un turno del agente: guardarlos
            # los deja contaminando el contexto de todos los mensajes que vengan despues.
            if es_respuesta_real:
                await guardar_mensaje(msg.telefono, "user", msg.texto)
                # El historial solo guarda texto: anotamos las fotos que se mandaron para
                # que Rory sepa despues que ya las envio (y cuales).
                nota = (
                    f"\n[Nota interna: en este mensaje se enviaron estos archivos: {', '.join(fotos_mandadas)}]"
                    if fotos_mandadas
                    else ""
                )
                await guardar_mensaje(msg.telefono, "assistant", respuesta + nota)

            logger.info(f"Respuesta enviada a {msg.telefono}: {respuesta}")

        except Exception as e:  # noqa: BLE001
            logger.exception(f"Error procesando el mensaje de {msg.telefono}: {e}")
            await liberar_evento(evento_id)
            try:
                await proveedor.enviar_mensaje(msg.telefono, obtener_mensaje_error(), msg.contexto)
            except Exception:  # noqa: BLE001
                logger.error("Tampoco se pudo avisarle al cliente del error")
