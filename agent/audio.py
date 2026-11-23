# agent/audio.py — Transcripcion de notas de voz de clientes (ElevenLabs Scribe)

"""
Convierte una nota de voz en texto. Necesita ELEVENLABS_API_KEY; sin ella no hace nada y
Rory sigue pidiendo que le escriban el mensaje.
"""

import logging
import os

import httpx

logger = logging.getLogger("agentkit")

MAX_BYTES = 3_000_000  # ~ varios minutos de nota de voz; evita gastar de mas en audios enormes


async def transcribir(audio: bytes, nombre: str = "nota.ogg") -> str | None:
    clave = os.getenv("ELEVENLABS_API_KEY", "").strip()
    if not clave:
        logger.warning("Nota de voz sin transcribir: falta ELEVENLABS_API_KEY")
        return None
    if not audio or len(audio) > MAX_BYTES:
        logger.warning(f"Nota de voz omitida por tamano ({len(audio or b'')} bytes)")
        return None
    modelo = os.getenv("ELEVENLABS_STT_MODEL") or "scribe_v2"
    try:
        async with httpx.AsyncClient(timeout=60.0) as cliente:
            r = await cliente.post(
                "https://api.elevenlabs.io/v1/speech-to-text",
                headers={"xi-api-key": clave},
                data={"model_id": modelo},
                files={"file": (nombre, audio, "audio/ogg")},
            )
    except httpx.HTTPError as e:
        logger.error(f"Error de red transcribiendo audio: {type(e).__name__}")
        return None
    if r.status_code != 200:
        logger.error(f"ElevenLabs rechazo la transcripcion [{r.status_code}]: {r.text[:300]}")
        return None
    texto = (r.json().get("text") or "").strip()
    return texto or None
