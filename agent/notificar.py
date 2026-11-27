# agent/notificar.py — Avisos al equipo por Telegram

"""
Manda un aviso por Telegram cuando Rory necesita apoyo de una persona.
Variables: TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID. Sin ellas no hace nada.
"""

import logging
import os

import httpx

logger = logging.getLogger("agentkit")


async def avisar_por_telegram(texto: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        logger.warning("Aviso al equipo no enviado: faltan TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": texto, "disable_web_page_preview": True},
            )
        if r.status_code != 200:
            logger.error(f"Telegram respondio {r.status_code}: {r.text[:200]}")
            return False
        return True
    except Exception as e:  # noqa: BLE001
        logger.error(f"No se pudo avisar por Telegram: {type(e).__name__}")
        return False


def enlace_cliente(telefono: str, plataforma: str) -> str:
    if plataforma == "whatsapp":
        return f"https://wa.me/{telefono}"
    return f"{plataforma.capitalize()} (abre el chat desde Zernio o Meta Business Suite)"


# ── Botones ──────────────────────────────────────────────────────────────────
# Un aviso de texto obliga a Luisito a sentarse frente a una pantalla para contestar.
# Con botones contesta desde el telefono, manejando a Puebla, de un toque.


def _api(metodo: str) -> str | None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    return f"https://api.telegram.org/bot{token}/{metodo}" if token else None


async def avisar_con_botones(texto: str, opciones: list[dict]) -> int:
    """
    Manda un aviso con botones y devuelve el id del mensaje (0 si fallo).
    Las opciones son [{"clave": "...", "etiqueta": "..."}]; la clave vuelve en el toque.
    El id sirve para editar el mensaje despues, cuando ya se contesto.
    """
    url, chat = _api("sendMessage"), os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not url or not chat:
        logger.warning("Aviso con botones no enviado: faltan TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
        return 0

    # callback_data tiene un tope de 64 bytes en Telegram, asi que la clave va corta.
    teclado = [[{"text": o["etiqueta"], "callback_data": f"e:{o['clave']}"[:64]}] for o in opciones]
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.post(url, json={
                "chat_id": chat, "text": texto, "disable_web_page_preview": True,
                "reply_markup": {"inline_keyboard": teclado},
            })
        if r.status_code != 200:
            logger.error(f"Telegram respondio {r.status_code}: {r.text[:200]}")
            return 0
        return int(r.json().get("result", {}).get("message_id") or 0)
    except Exception as e:  # noqa: BLE001
        logger.error(f"No se pudo mandar el aviso con botones: {type(e).__name__}")
        return 0


async def editar_aviso(mensaje_id: int, texto: str) -> bool:
    """Reescribe un aviso ya mandado y le quita los botones, para que no se conteste dos veces."""
    url, chat = _api("editMessageText"), os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not url or not chat or not mensaje_id:
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.post(url, json={
                "chat_id": chat, "message_id": int(mensaje_id), "text": texto,
                "disable_web_page_preview": True, "reply_markup": {"inline_keyboard": []},
            })
        return r.status_code == 200
    except Exception as e:  # noqa: BLE001
        logger.error(f"No se pudo editar el aviso: {type(e).__name__}")
        return False


async def confirmar_toque(callback_id: str, texto: str = "") -> bool:
    """
    Le contesta a Telegram que el toque llego. Sin esto el boton se queda girando
    en el telefono unos segundos aunque todo haya salido bien.
    """
    url = _api("answerCallbackQuery")
    if not url or not callback_id:
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.post(url, json={"callback_query_id": callback_id, "text": texto[:200]})
        return r.status_code == 200
    except Exception as e:  # noqa: BLE001
        logger.error(f"No se pudo confirmar el toque: {type(e).__name__}")
        return False


def chat_autorizado(chat_id) -> bool:
    """
    True solo si el chat es el de Luisito.

    Es la unica barrera entre un boton que aprueba cosas y cualquiera que le escriba
    al bot: el token del bot es publico en el sentido de que basta conocerlo para
    hablarle, pero solo este chat puede decidir.
    """
    esperado = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    return bool(esperado) and str(chat_id).strip() == esperado


async def registrar_webhook() -> tuple[bool, str]:
    """
    Le dice a Telegram a donde mandar los toques de los botones.

    Lo hace la app al arrancar, con el token que ya tiene en el entorno, para que
    nadie tenga que copiar el token a mano a una terminal. setWebhook es idempotente:
    llamarlo en cada arranque no rompe nada.

    Solo pide callback_query: los mensajes que alguien le escriba al bot no nos
    interesan y no tiene caso recibirlos.
    """
    url = _api("setWebhook")
    secreto = (os.getenv("TELEGRAM_WEBHOOK_SECRET") or "").strip()
    base = (os.getenv("PUBLIC_BASE_URL") or "").strip()
    if not base and os.getenv("RAILWAY_PUBLIC_DOMAIN"):
        base = f"https://{os.environ['RAILWAY_PUBLIC_DOMAIN']}"

    if not url:
        return False, "falta TELEGRAM_BOT_TOKEN"
    if not secreto:
        return False, "falta TELEGRAM_WEBHOOK_SECRET: sin el, el endpoint no se registra"
    if not base:
        return False, "no hay URL publica (PUBLIC_BASE_URL ni RAILWAY_PUBLIC_DOMAIN)"

    destino = f"{base.rstrip('/')}/telegram"
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            r = await http.post(url, json={
                "url": destino,
                "secret_token": secreto,
                "allowed_updates": ["callback_query"],
                # Al registrar por primera vez puede haber toques viejos encolados:
                # entregarlos resolveria esperas que ya no tienen sentido.
                "drop_pending_updates": True,
            })
    except Exception as e:  # noqa: BLE001
        return False, f"no se pudo contactar a Telegram: {type(e).__name__}"

    if r.status_code != 200:
        return False, f"Telegram respondio {r.status_code}: {r.text[:200]}"
    return True, f"webhook en {destino}"
