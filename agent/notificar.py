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
