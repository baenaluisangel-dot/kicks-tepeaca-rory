# agent/main.py — Servidor FastAPI + Webhook de WhatsApp
# Generado por AgentKit

"""
Servidor principal del agente.
Funciona con cualquier proveedor (Zernio, Meta) gracias a la capa de providers.
"""

import asyncio
import logging
import re
import time
import os
from collections import defaultdict
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from agent.brain import MAX_FOTOS_POR_RESPUESTA, generar_respuesta, obtener_mensaje_error
from agent.catalogo import (
    ARCHIVO_CATALOGO_PDF,
    ARCHIVO_PAQUETES_PDF,
    url_paquetes_pdf,
    obtener_producto,
    pie_de_foto,
    url_catalogo_pdf,
    url_foto,
)
from agent.memory import (
    guardar_mensaje,
    esta_pausada,
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

        if await esta_pausada(msg.telefono):
            logger.info(f"Mensaje de {msg.telefono} sin contestar: Rory esta en pausa ahi")
            continue

        logger.info(f"Mensaje de {msg.telefono}: {msg.texto}")
        tareas.add_task(procesar_mensaje, msg)
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
        await pausar(msg.telefono, HORAS_PAUSA_HUMANO)
        logger.info(f"{msg.telefono}: respondio una persona; Rory en pausa {HORAS_PAUSA_HUMANO} h")


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
            ya_mando_pdf = any("paquetes en PDF" in m["content"] for m in historial)
            if es_puebla and not ya_mando_pdf:
                if (await enviar_catalogo()).startswith("OK"):
                    texto_para_ia += (
                        "\n\n[Nota interna: el sistema ya le envió el PDF de paquetes en este "
                        "turno; no uses enviar_catalogo.]"
                    )

            respuesta, es_respuesta_real = await generar_respuesta(
                texto_para_ia,
                historial,
                None if es_puebla else enviar_foto,
                enviar_catalogo,
                plataforma,
                modo,
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
