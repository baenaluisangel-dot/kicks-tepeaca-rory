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
    ARCHIVO_PAQUETES_PDF,
    PAQUETES_PUEBLA,
    url_paquetes_pdf,
    obtener_producto,
    pie_de_foto,
    url_catalogo_pdf,
    url_foto,
)
from agent.memory import (
    guardar_mensaje,
    borrar_pendientes,
    cargar_pendientes,
    esta_pausada,
    guardar_pendiente,
    listar_atencion,
    marcar_necesita_persona,
    necesita_persona,
    listar_avisos,
    metricas,
    registrar_aviso,
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
from agent.notificar import avisar_por_telegram, enlace_cliente
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
    asyncio.create_task(_bucle_resumen_diario())
    logger.info(f"Servidor AgentKit escuchando en el puerto {PORT}")

    global estado_proveedor
    if proveedor is not None:
        logger.info(f"Proveedor de WhatsApp: {proveedor.__class__.__name__}")
        ok, detalle = await proveedor.verificar_conexion()
        estado_proveedor = {"ok": ok, "detalle": detalle}
        logger.info(f"Conexion con el proveedor: {'OK' if ok else 'ERROR'} — {detalle}")
    else:
        logger.error(f"Proveedor de WhatsApp NO configurado: {error_configuracion}")

    yield


_ultimos_textos: dict[tuple[str, str], float] = {}
_ultimo_recordatorio: dict[str, float] = {}

app = FastAPI(title="AgentKit — WhatsApp AI Agent", version="2.0.0", lifespan=lifespan)

# Fotos de los productos. WhatsApp las descarga de aqui, asi que tienen que ser publicas.
app.mount("/fotos", StaticFiles(directory="static/fotos"), name="fotos")
# Catalogo de juguetes en PDF (se manda como documento cuando lo piden).
app.mount("/catalogo", StaticFiles(directory="static/catalogo"), name="catalogo")


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
        if not msg.texto.strip():
            continue

        # La entrega es "al menos una vez": el mismo evento puede llegar dos veces
        evento_id = msg.contexto.get("evento_id") or msg.mensaje_id
        if evento_id and not await marcar_evento_procesado(evento_id):
            logger.info(f"Evento repetido, se ignora: {evento_id}")
            continue

        # Instagram a veces entrega el mismo mensaje dos veces con ids distintos (sobre
        # todo el predeterminado de los anuncios). Mismo cliente + mismo texto en menos
        # de 60 s = duplicado, no se contesta dos veces.
        ahora = time.monotonic()
        clave = (msg.telefono, msg.texto.strip().lower())
        if ahora - _ultimos_textos.get(clave, -1e9) < 60:
            logger.info(f"Mensaje duplicado de {msg.telefono}, se ignora")
            continue
        _ultimos_textos[clave] = ahora
        if len(_ultimos_textos) > 2000:
            for k in [k for k, t in _ultimos_textos.items() if ahora - t > 60]:
                _ultimos_textos.pop(k, None)

        await registrar_mensaje_cliente(
            msg.telefono, msg.contexto.get("plataforma", "whatsapp"), msg.texto
        )
        if await esta_pausada(msg.telefono):
            logger.info(f"Mensaje de {msg.telefono} sin contestar: Rory esta en pausa ahi")
            # El cliente sigue escribiendo y esta esperando a una persona: recordatorio
            # (maximo uno cada 10 minutos por cliente).
            if await necesita_persona(msg.telefono) and ahora - _ultimo_recordatorio.get(msg.telefono, -1e9) > 600:
                _ultimo_recordatorio[msg.telefono] = ahora
                plat = msg.contexto.get("plataforma", "whatsapp")
                await avisar_por_telegram(
                    f"💬 Un cliente que espera a una persona volvió a escribir.\n"
                    f"Cliente: {enlace_cliente(msg.telefono, plat)}\nMensaje: {msg.texto[:300]}"
                )
            continue

        logger.info(f"Mensaje de {msg.telefono}: {msg.texto}")
        await _encolar(msg)
        encolados += 1

    return {"status": "ok", "encolados": encolados}


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


async def _trabajador(telefono: str):
    try:
        while _pendientes.get(telefono):
            lo, hi = _espera_recuperado.pop(telefono, (DELAY_MIN, max(DELAY_MIN, DELAY_MAX)))
            await asyncio.sleep(random.uniform(lo, hi))
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
                if es_puebla and not producto_id.startswith("toy-story"):
                    return "ERROR: en Puebla solo se muestran productos de Toy Story."
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

            async def enviar_catalogo() -> str:
                """La usa Claude (herramienta enviar_catalogo) para mandar el PDF del catalogo."""
                url = url_paquetes_pdf() if es_puebla else url_catalogo_pdf()
                archivo = ARCHIVO_PAQUETES_PDF if es_puebla else ARCHIVO_CATALOGO_PDF
                if not url:
                    return "ERROR: no hay URL publica configurada para el catalogo."
                # Se manda el PDF como archivo (en Instagram tambien). Si Instagram lo
                # rechaza, se cae al enlace como ultimo recurso.
                # En Instagram el pie de foto sale como un globo aparte (ruido): sin texto.
                pie = "Catálogo de paquetes Toy Story 🎁" if es_puebla else "Catálogo de juguetes de Kicks Tepeaca 🎁"
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
                    fotos_mandadas.append("el catálogo de paquetes en PDF" if es_puebla else "el catálogo de juguetes en PDF")
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
                    texto_para_ia += (
                        "\n\n[Nota interna: el sistema ya le envió el PDF de paquetes en este "
                        "turno; no uses enviar_catalogo.]"
                    )

            async def avisar_equipo(motivo: str, tipo: str = "otro") -> str:
                """La usa Claude: deja la conversacion marcada para una persona y pausa a Rory."""
                await marcar_necesita_persona(msg.telefono, True, motivo)
                await registrar_aviso(msg.telefono, tipo, motivo)
                await pausar(msg.telefono, HORAS_PAUSA_AVISO)
                logger.info(f"{msg.telefono}: necesita persona ({tipo}) — {motivo}")
                icono = {"pedido": "🟢 PEDIDO LISTO", "duda_producto": "🟠 Duda de producto",
                         "fuera_de_catalogo": "🟡 Pide algo que no tenemos", "queja_o_problema": "🔴 Queja o problema"
                         }.get(tipo, "🔵 Necesita apoyo")
                await avisar_por_telegram(
                    f"{icono}\nCliente: {enlace_cliente(msg.telefono, plataforma)}\n"
                    f"Motivo: {motivo}\nÚltimo mensaje: {msg.texto[:300]}"
                )
                return "OK: el equipo quedo avisado. Dile al cliente que un companero le contesta por este chat."

            respuesta, es_respuesta_real = await generar_respuesta(
                texto_para_ia,
                historial,
                enviar_foto,
                enviar_catalogo,
                plataforma,
                modo,
                avisar_equipo,
                enviar_fotos_paquete if es_puebla else None,
            )

            enviado = await proveedor.enviar_mensaje(msg.telefono, respuesta, msg.contexto)

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


# ── Panel del equipo ─────────────────────────────────────────────────────

PANEL_TOKEN = os.getenv("PANEL_TOKEN", "")


def _panel_autorizado(token: str) -> bool:
    import hmac
    return bool(PANEL_TOKEN) and hmac.compare_digest(token or "", PANEL_TOKEN)


def _zona_mx():
    """Hora de Mexico (Puebla): UTC-6 fijo, sin horario de verano desde 2022."""
    from datetime import timedelta, timezone
    return timezone(timedelta(hours=-6))


def _inicio_de_hoy():
    from datetime import datetime
    z = _zona_mx()
    return datetime.now(z).replace(hour=0, minute=0, second=0, microsecond=0)


NOMBRES_TIPO = {
    "pedido": "🟢 Pedido listo", "duda_producto": "🟠 Duda de producto",
    "fuera_de_catalogo": "🟡 Pide algo que no tenemos", "queja_o_problema": "🔴 Queja o problema",
    "otro": "🔵 Otro",
}


async def _resumen_texto() -> str:
    """Resumen del dia (hora de Mexico) para Telegram y para el panel."""
    desde = _inicio_de_hoy()
    m = await metricas(desde)
    lineas = [f"📊 Resumen de hoy ({desde.strftime('%d/%m')})"]
    for canal, c in m["por_canal"].items():
        pct = round(100 * c["respondieron"] / c["nuevas"]) if c["nuevas"] else 0
        lineas.append(f"{canal.capitalize()}: {c['nuevas']} conversaciones nuevas, {c['respondieron']} respondieron ({pct}%)")
    if not m["por_canal"]:
        lineas.append("Sin conversaciones nuevas.")
    if m["avisos_por_tipo"]:
        lineas.append("Pasadas al equipo: " + ", ".join(f"{NOMBRES_TIPO.get(t, t)} {n}" for t, n in m["avisos_por_tipo"].items()))
    esperando = [c for c in await listar_atencion() if c["necesita_persona"]]
    lineas.append(f"Esperando a una persona ahora: {len(esperando)}")
    for c in esperando[:10]:
        lineas.append(f"• {enlace_cliente(c['telefono'], c['plataforma'])} — {c['motivo'][:90]}")
    return "\n".join(lineas)


async def _bucle_resumen_diario():
    """Manda el resumen por Telegram todos los dias a HORA_RESUMEN (hora de Mexico)."""
    from datetime import datetime, timedelta
    hora = int(os.getenv("HORA_RESUMEN") or "21")
    while True:
        z = _zona_mx()
        ahora_mx = datetime.now(z)
        objetivo = ahora_mx.replace(hour=hora, minute=0, second=0, microsecond=0)
        if objetivo <= ahora_mx:
            objetivo += timedelta(days=1)
        await asyncio.sleep((objetivo - ahora_mx).total_seconds())
        try:
            await avisar_por_telegram(await _resumen_texto())
        except Exception as e:  # noqa: BLE001
            logger.error(f"No se pudo mandar el resumen diario: {e}")


def _esperando_txt(desde) -> str:
    from datetime import datetime, timezone
    d = desde if desde.tzinfo else desde.replace(tzinfo=timezone.utc)
    mins = int((datetime.now(timezone.utc) - d).total_seconds() // 60)
    if mins < 60:
        return f"{mins} min"
    return f"{mins // 60} h {mins % 60} min" if mins < 1440 else f"{mins // 1440} d"


def _cliente_html(c: dict) -> str:
    tel = c["telefono"]
    if c["plataforma"] == "whatsapp":
        return f'<a href="https://wa.me/{html.escape(tel)}" target="_blank">+{html.escape(tel)}</a>'
    return f'{html.escape(c["plataforma"].capitalize())} ({html.escape(tel.split(":")[-1][-6:])})'


@app.get("/panel", response_class=HTMLResponse)
async def panel(token: str = ""):
    """Panel del equipo: numeros del dia, quien espera a una persona y todas las conversaciones."""
    if not _panel_autorizado(token):
        raise HTTPException(status_code=404)
    from urllib.parse import quote
    t = html.escape(token, quote=True)
    desde = _inicio_de_hoy()
    m = await metricas(desde)
    ultimos = await ultimo_aviso_por_telefono()
    todas = await listar_atencion()

    def boton(tel, accion, texto):
        return (
            f'<form method="post" action="/panel/accion" style="display:inline">'
            f'<input type="hidden" name="token" value="{t}">'
            f'<input type="hidden" name="telefono" value="{html.escape(tel, quote=True)}">'
            f'<input type="hidden" name="accion" value="{accion}"><button>{texto}</button></form> '
        )

    def ver(tel):
        return f'<a href="/panel/chat?token={quote(token)}&tel={quote(tel)}">Ver chat</a>'

    # tarjetas
    tarjetas = ""
    for canal, c in m["por_canal"].items():
        pct = round(100 * c["respondieron"] / c["nuevas"]) if c["nuevas"] else 0
        tarjetas += (f'<div class="card"><b>{html.escape(canal.capitalize())}</b><br>{c["nuevas"]} nuevas hoy<br>'
                     f'{c["respondieron"]} respondieron ({pct}%)</div>')
    avisos_t = "".join(f"<div>{NOMBRES_TIPO.get(k, k)}: <b>{v}</b></div>" for k, v in m["avisos_por_tipo"].items()) or "Ninguno todavía"
    tarjetas += f'<div class="card"><b>Pasadas al equipo hoy</b><br>{avisos_t}</div>'

    # esperando persona
    esp = [c for c in todas if c["necesita_persona"]]
    filas_esp = ""
    for c in esp:
        u = ultimos.get(c["telefono"], {})
        filas_esp += (
            f'<tr class="alerta"><td>{NOMBRES_TIPO.get(u.get("tipo", "otro"), "🔵 Otro")}<br><small>{html.escape(c["motivo"])}</small></td>'
            f'<td>{_cliente_html(c)}</td><td>{html.escape(c["texto"])}</td><td>{_esperando_txt(c["ultimo_en"])}</td>'
            f'<td>{ver(c["telefono"])} {boton(c["telefono"], "atendido", "Ya lo atendí")}{boton(c["telefono"], "reactivar", "Reactivar Rory")}</td></tr>'
        )
    filas_esp = filas_esp or '<tr><td colspan="5">Nadie esperando 🎉</td></tr>'

    # lo que Rory no sabe (hoy)
    vacios = [a for a in m["avisos"] if a["tipo"] in ("duda_producto", "fuera_de_catalogo")]
    lista_vacios = "".join(
        f'<li>{NOMBRES_TIPO.get(a["tipo"], "")} — {html.escape(a["motivo"])}</li>' for a in vacios[:30]
    ) or "<li>Nada por ahora</li>"

    # todas
    filas = ""
    for c in todas[:150]:
        estado = "🔴 Necesita persona" if c["necesita_persona"] else "⏸️ Rory en pausa" if c["pausada"] else "🤖 Rory atiende"
        hora = c["ultimo_en"].astimezone(_zona_mx()).strftime("%d/%m %H:%M")
        filas += (f'<tr><td>{estado}</td><td>{_cliente_html(c)}</td><td>{html.escape(c["texto"])}</td><td>{hora}</td>'
                  f'<td>{ver(c["telefono"])} {boton(c["telefono"], "pausar", "Yo me encargo")}{boton(c["telefono"], "reactivar", "Reactivar Rory")}</td></tr>')

    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        "<title>Rory — panel</title>"
        "<style>body{font-family:system-ui;margin:16px;max-width:1100px}table{border-collapse:collapse;width:100%;margin-bottom:24px}"
        "td,th{border-bottom:1px solid #ddd;padding:8px;text-align:left;vertical-align:top;font-size:14px}"
        "tr.alerta{background:#ffe8e8}button{padding:4px 8px;margin:2px 0}"
        ".cards{display:flex;gap:12px;flex-wrap:wrap;margin:12px 0 24px}.card{border:1px solid #ddd;border-radius:10px;padding:12px 16px;min-width:170px}</style>"
        f"<h2>Rory — panel del equipo</h2><p><a href='?token={t}'>Actualizar</a></p>"
        f"<div class='cards'>{tarjetas}</div>"
        f"<h3>Esperando a una persona ({len(esp)})</h3>"
        "<table><tr><th>Tipo</th><th>Cliente</th><th>Último mensaje</th><th>Espera</th><th></th></tr>" + filas_esp + "</table>"
        f"<h3>Lo que Rory no supo contestar hoy</h3><ul>{lista_vacios}</ul>"
        "<h3>Todas las conversaciones</h3>"
        "<table><tr><th>Estado</th><th>Cliente</th><th>Último mensaje del cliente</th><th>Hora</th><th></th></tr>" + filas + "</table>"
    )


@app.get("/panel/chat", response_class=HTMLResponse)
async def panel_chat(token: str = "", tel: str = ""):
    """La conversacion completa de un cliente."""
    if not _panel_autorizado(token):
        raise HTTPException(status_code=404)
    from urllib.parse import quote
    msgs = await transcripcion(tel)
    burbujas = ""
    for x in msgs:
        quien = "Cliente" if x["role"] == "user" else "Rory"
        clase = "cli" if x["role"] == "user" else "rory"
        hora = x["hora"].astimezone(_zona_mx()).strftime("%d/%m %H:%M")
        burbujas += (f'<div class="b {clase}"><small>{quien} · {hora}</small><br>'
                     f'{html.escape(x["texto"]).replace(chr(10), "<br>")}</div>')
    burbujas = burbujas or "<p>Todavía no hay mensajes guardados de esta conversación.</p>"
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'><title>Rory — chat</title>"
        "<style>body{font-family:system-ui;margin:16px;max-width:720px}.b{padding:8px 12px;border-radius:12px;margin:8px 0;max-width:85%}"
        ".cli{background:#f1f1f1}.rory{background:#d9f2d9;margin-left:auto}small{color:#666}</style>"
        f"<p><a href='/panel?token={quote(token)}'>← Volver al panel</a></p><h3>{html.escape(tel)}</h3>" + burbujas
    )


@app.post("/panel/accion")
async def panel_accion(request: Request):
    form = await request.form()
    if not _panel_autorizado(str(form.get("token", ""))):
        raise HTTPException(status_code=404)
    tel, accion = str(form.get("telefono", "")), str(form.get("accion", ""))
    if accion == "reactivar":
        await reanudar(tel)
        await marcar_necesita_persona(tel, False)
    elif accion == "pausar":
        await pausar(tel)
    elif accion == "atendido":
        await marcar_necesita_persona(tel, False)
    from fastapi.responses import RedirectResponse
    return RedirectResponse(f"/panel?token={form.get('token')}", status_code=303)
