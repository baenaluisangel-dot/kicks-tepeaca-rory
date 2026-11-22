# agent/providers/zernio.py — Adaptador para Zernio
# Generado por AgentKit

"""
Zernio corre sobre la WhatsApp Cloud API de Meta: resuelve la conexion de la WhatsApp
Business Account, el inbox unificado y los webhooks firmados.

Documentacion: https://docs.zernio.com/platforms/whatsapp
"""

import hashlib
import hmac
import logging
import os

import httpx
from fastapi import Request

from agent.providers.base import MensajeEntrante, ProveedorWhatsApp

logger = logging.getLogger("agentkit")

BASE_URL_POR_DEFECTO = "https://zernio.com/api/v1"


class ProveedorZernio(ProveedorWhatsApp):
    """Proveedor de WhatsApp usando Zernio."""

    def __init__(self):
        self.api_key = os.getenv("ZERNIO_API_KEY", "")
        self.webhook_secret = os.getenv("ZERNIO_WEBHOOK_SECRET", "")
        # Opcional: solo se usa para el chequeo de conexion al arrancar.
        # Para responder, el account_id sale del propio webhook.
        self.account_id = os.getenv("ZERNIO_ACCOUNT_ID", "")
        # Segunda cuenta de Zernio (opcional), por ejemplo para Facebook Messenger.
        # Cada cuenta tiene su propia API key y su propio secreto de webhook.
        self.api_key_2 = os.getenv("ZERNIO_API_KEY_2", "")
        self.webhook_secret_2 = os.getenv("ZERNIO_WEBHOOK_SECRET_2", "")
        # Ojo con el "or": os.getenv(clave, default) solo usa el default si la clave NO
        # existe. Como el .env trae "ZERNIO_BASE_URL=" vacia, os.getenv devuelve "" y el
        # default nunca se aplicaria. El "or" cubre los dos casos.
        self.base_url = (os.getenv("ZERNIO_BASE_URL") or BASE_URL_POR_DEFECTO).rstrip("/")

        if not self.api_key:
            logger.warning("ZERNIO_API_KEY no esta configurada: el agente no va a poder responder")
        if not self.webhook_secret:
            logger.warning(
                "ZERNIO_WEBHOOK_SECRET no esta configurado: los webhooks NO se verifican. "
                "Sirve para probar, pero no lo dejes asi en produccion."
            )

    # ── Recibir ──────────────────────────────────────────────────────────

    async def verificar_firma(self, request: Request) -> bool:
        """Compara el header X-Zernio-Signature contra el HMAC-SHA256 del cuerpo crudo."""
        if not self.webhook_secret:
            return True  # modo pruebas, ya se advirtio al arrancar

        firma_recibida = request.headers.get("X-Zernio-Signature") or request.headers.get(
            "X-Late-Signature"
        )
        if not firma_recibida:
            logger.warning("Llego un webhook sin firma X-Zernio-Signature: rechazado")
            return False

        cuerpo = await request.body()
        # Se prueba con el secreto de cada cuenta; el que coincida dice de cual viene.
        secretos = [(1, self.webhook_secret)]
        if self.webhook_secret_2:
            secretos.append((2, self.webhook_secret_2))

        for cuenta, secreto in secretos:
            if not secreto:
                continue
            firma_esperada = hmac.new(
                secreto.encode("utf-8"), cuerpo, hashlib.sha256
            ).hexdigest()
            # compare_digest sobre str exige ASCII puro; los headers pueden traer otros
            # bytes y eso tiraria TypeError (un 500 y siete reintentos del proveedor).
            try:
                if hmac.compare_digest(firma_esperada, firma_recibida):
                    request.state.zernio_cuenta = cuenta
                    return True
            except TypeError:
                logger.warning("La firma del webhook trae caracteres invalidos: rechazado")
                return False

        logger.warning("Firma de webhook invalida: rechazado")
        return False

    def _api_key(self, contexto: dict | None) -> str:
        """API key de la cuenta de Zernio de la que vino el mensaje."""
        if (contexto or {}).get("cuenta_zernio") == 2 and self.api_key_2:
            return self.api_key_2
        return self.api_key

    async def parsear_webhook(self, request: Request) -> list[MensajeEntrante]:
        """Normaliza el evento message.received de Zernio."""
        payload = await request.json()

        evento = payload.get("event")
        if evento == "message.sent":
            return self._parsear_enviado(payload, request)
        if evento != "message.received":
            # message.sent, message.delivered, message.read, etc. no se contestan
            logger.info(f"Evento ignorado: {evento}")
            return []

        mensaje = payload.get("message") or {}
        plataforma = mensaje.get("platform")
        if plataforma == "messenger":
            plataforma = "facebook"
        if plataforma not in ("whatsapp", "instagram", "facebook"):
            return []

        # Si otra app controla la conversacion (standby), no contestamos.
        if (payload.get("metadata") or {}).get("standby"):
            return []

        remitente = mensaje.get("sender") or {}
        texto = mensaje.get("text") or ""

        # Datos del anuncio de origen, si Zernio los manda (para distinguir campanas).
        # Diagnostico: se loguean solo los NOMBRES de los campos, no su contenido.
        referral = (
            mensaje.get("referral")
            or mensaje.get("adReferral")
            or (payload.get("metadata") or {}).get("referral")
            or ""
        )
        if plataforma in ("instagram", "facebook"):
            logger.info(
                f"Campos del mensaje: {sorted(mensaje.keys())} | "
                f"metadata: {sorted((payload.get('metadata') or {}).keys())} | "
                f"referral: {'si' if referral else 'no'}"
            )

        if plataforma == "instagram":
            # Instagram no tiene telefono: el id del usuario, con prefijo para que
            # nunca choque con un numero de WhatsApp en el historial.
            telefono = "ig:" + (remitente.get("id") or "")
            if not texto.strip() and mensaje.get("attachments"):
                # Cuando llegan de un anuncio, Instagram manda primero el anuncio/reel
                # (sin texto) y luego el texto ("Quiero más información"). Contestar a
                # los dos duplica la respuesta, asi que lo que viene sin texto se ignora.
                logger.info("Mensaje de Instagram sin texto (adjunto/anuncio): se ignora")
                return []
        elif plataforma == "facebook":
            telefono = "fb:" + (remitente.get("id") or "")
            if not texto.strip():
                logger.info("Mensaje de Facebook sin texto (adjunto): se ignora")
                return []
        else:
            # phoneNumber viene en E.164 con "+". Desde abril de 2026 puede faltar
            # (los usuarios con username de WhatsApp no exponen su numero), asi que
            # caemos al identificador que Meta si garantiza.
            telefono = (remitente.get("phoneNumber") or "").lstrip("+")
            if not telefono:
                telefono = remitente.get("businessScopedUserId") or remitente.get("id") or ""

        cuenta = payload.get("account") or {}

        return [
            MensajeEntrante(
                telefono=telefono,
                texto=texto,
                mensaje_id=mensaje.get("platformMessageId") or mensaje.get("id") or "",
                # Zernio marca la direccion: solo contestamos lo que entra
                es_propio=mensaje.get("direction") != "incoming",
                contexto={
                    "evento_id": payload.get("id", ""),
                    "conversation_id": mensaje.get("conversationId", ""),
                    "account_id": cuenta.get("accountId") or cuenta.get("id", ""),
                    "plataforma": plataforma,
                    "referral": str(referral)[:500],
                    "cuenta_zernio": getattr(request.state, "zernio_cuenta", 1),
                },
            )
        ]

    def _parsear_enviado(self, payload: dict, request: Request) -> list[MensajeEntrante]:
        """
        message.sent: un mensaje que sale de la cuenta del negocio. Sirve para que el
        equipo pueda pausar a Rory escribiendo en el chat. No se contesta nunca.
        """
        mensaje = payload.get("message") or {}
        plataforma = mensaje.get("platform")
        if plataforma == "messenger":
            plataforma = "facebook"
        if plataforma not in ("whatsapp", "instagram", "facebook"):
            return []
        # Lo que manda Rory por la API no cuenta como intervencion humana
        if mensaje.get("sentVia") == "api" or mensaje.get("source") == "cloud_api":
            return []
        # El destinatario viene en la conversacion, no en sender (sender es el negocio)
        conv = payload.get("conversation") or {}
        contacto = (
            conv.get("participantId") or conv.get("contactId")
            or (conv.get("participant") or {}).get("id")
            or (conv.get("contact") or {}).get("id") or ""
        )
        telefono_wa = (
            (conv.get("participant") or {}).get("phoneNumber")
            or (conv.get("contact") or {}).get("phoneNumber")
            or conv.get("participantPhone") or ""
        )
        if plataforma == "instagram":
            telefono = "ig:" + contacto
        elif plataforma == "facebook":
            telefono = "fb:" + contacto
        else:
            telefono = str(telefono_wa).lstrip("+") or contacto
        logger.info(
            f"message.sent ({plataforma}) campos conversation: {sorted(conv.keys())} | "
            f"sentVia={mensaje.get('sentVia')} source={mensaje.get('source')} "
            f"destino_resuelto={'si' if telefono.strip(':') and telefono not in ('ig:', 'fb:') else 'no'}"
        )
        if telefono in ("", "ig:", "fb:"):
            return []
        humano = mensaje.get("sentVia") == "human" or mensaje.get("source") == "whatsapp_business_app"
        return [
            MensajeEntrante(
                telefono=telefono,
                texto=mensaje.get("text") or "",
                mensaje_id=mensaje.get("platformMessageId") or mensaje.get("id") or "",
                es_propio=True,
                contexto={
                    "evento_id": payload.get("id", ""),
                    "plataforma": plataforma,
                    "humano": humano,
                },
            )
        ]

    async def hubo_respuesta_humana(self, contexto: dict, desde) -> bool:
        """
        True si una persona del equipo escribio en la conversacion despues de `desde`
        (datetime UTC). Respaldo por si el webhook message.sent no llega: se consulta la
        API. Si no se puede saber, devuelve False (Rory contesta como siempre).
        """
        conversation_id = contexto.get("conversation_id")
        api_key = self._api_key(contexto)
        if not conversation_id or not api_key:
            return False
        try:
            async with httpx.AsyncClient(timeout=15.0) as cliente:
                r = await cliente.get(
                    f"{self.base_url}/inbox/conversations/{conversation_id}/messages",
                    params={"sortOrder": "desc", "limit": 15},
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            if r.status_code != 200:
                logger.warning(f"No se pudo revisar la conversacion ({r.status_code})")
                return False
            datos = r.json()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"No se pudo revisar la conversacion: {type(e).__name__}")
            return False

        lista = datos if isinstance(datos, list) else (
            datos.get("messages") or datos.get("data") or []
        )
        if lista and not getattr(self, "_log_campos_lista", False):
            self._log_campos_lista = True
            logger.info(f"Campos de un mensaje de la lista de Zernio: {sorted(lista[0].keys())}")
        for m in lista:
            if not isinstance(m, dict) or m.get("direction") != "outgoing":
                continue
            humano = m.get("sentVia") == "human" or m.get("source") == "whatsapp_business_app"
            if not humano:
                continue
            marca = m.get("sentAt") or m.get("createdAt") or m.get("timestamp") or ""
            try:
                from datetime import datetime, timezone
                t = datetime.fromisoformat(str(marca).replace("Z", "+00:00"))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if t >= desde:
                return True
        return False

    # ── Enviar ───────────────────────────────────────────────────────────

    async def enviar_mensaje(
        self, telefono: str, mensaje: str, contexto: dict | None = None
    ) -> bool:
        """Responde dentro de la conversacion que abrio el cliente."""
        contexto = contexto or {}
        conversation_id = contexto.get("conversation_id")
        account_id = contexto.get("account_id") or self.account_id

        api_key = self._api_key(contexto)
        if not api_key:
            logger.error("No se puede enviar: falta ZERNIO_API_KEY")
            return False
        if not conversation_id or not account_id:
            logger.error(
                "No se puede enviar: el mensaje no trae conversation_id o account_id"
            )
            return False

        url = f"{self.base_url}/inbox/conversations/{conversation_id}/messages"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        # Si el webhook se reintenta, esta clave evita que Zernio mande la respuesta dos veces
        evento_id = contexto.get("evento_id")
        if evento_id:
            headers["Idempotency-Key"] = f"agentkit-{evento_id}"

        try:
            async with httpx.AsyncClient(timeout=30.0) as cliente:
                r = await cliente.post(
                    url,
                    json={"accountId": account_id, "message": mensaje},
                    headers=headers,
                )
        except httpx.HTTPError as e:
            logger.error(f"Error de red hablando con Zernio: {e}")
            return False

        if r.status_code == 200:
            return True

        # Zernio responde {"error": ..., "type": ..., "code": ...}.
        # Loguear los tres ahorra muchisimo tiempo de diagnostico.
        detalle = r.text[:500]
        try:
            cuerpo = r.json()
            detalle = (
                f"{cuerpo.get('error')} "
                f"(type={cuerpo.get('type')}, code={cuerpo.get('code')})"
            )
        except ValueError:
            pass
        logger.error(
            f"Zernio rechazo el envio [{r.status_code}]: {detalle} "
            f"(cuenta_zernio={contexto.get('cuenta_zernio')}, "
            f"plataforma={contexto.get('plataforma')}, account_id={account_id}, "
            f"key_2_configurada={bool(self.api_key_2)})"
        )
        return False

    async def enviar_imagen(
        self, telefono: str, url: str, texto: str, contexto: dict | None = None
    ) -> bool:
        """
        Manda una imagen con pie de foto dentro de la conversacion.
        La URL tiene que ser publica y devolver el archivo (JPEG o PNG, maximo 5 MB).
        """
        contexto = contexto or {}
        conversation_id = contexto.get("conversation_id")
        account_id = contexto.get("account_id") or self.account_id

        api_key = self._api_key(contexto)
        if not api_key or not conversation_id or not account_id:
            logger.error("No se puede enviar la imagen: faltan API key, conversation_id o account_id")
            return False

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        evento_id = contexto.get("evento_id")
        if evento_id:
            # Una clave distinta por foto, para que no choque con la del texto ni con otras fotos
            # Se usa la ruta completa de la foto (ej: buzz-chico/01.jpg), no solo el nombre del
            # archivo: muchas fotos se llaman igual (01.jpg) y Zernio rechaza con 422 si la
            # misma clave se reutiliza con una solicitud distinta.
            ruta = url.split("/fotos/", 1)[-1].replace("/", "-")
            headers["Idempotency-Key"] = f"agentkit-{evento_id}-{ruta}"

        try:
            async with httpx.AsyncClient(timeout=30.0) as cliente:
                r = await cliente.post(
                    f"{self.base_url}/inbox/conversations/{conversation_id}/messages",
                    json={
                        "accountId": account_id,
                        "message": texto,
                        "attachmentUrl": url,
                        "attachmentType": "image",
                    },
                    headers=headers,
                )
        except httpx.HTTPError as e:
            logger.error(f"Error de red mandando imagen a Zernio: {e}")
            return False

        if r.status_code == 200:
            return True
        logger.error(f"Zernio rechazo la imagen [{r.status_code}]: {r.text[:500]}")
        return False

    async def enviar_video(
        self, telefono: str, url: str, texto: str, contexto: dict | None = None
    ) -> bool:
        """Manda un video MP4 (H.264/AAC) con texto. La URL debe ser publica."""
        contexto = contexto or {}
        conversation_id = contexto.get("conversation_id")
        account_id = contexto.get("account_id") or self.account_id
        api_key = self._api_key(contexto)
        if not api_key or not conversation_id or not account_id:
            logger.error("No se puede enviar el video: faltan API key, conversation_id o account_id")
            return False
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        evento_id = contexto.get("evento_id")
        if evento_id:
            headers["Idempotency-Key"] = f"agentkit-{evento_id}-video-{url.rsplit('/', 1)[-1]}"
        try:
            async with httpx.AsyncClient(timeout=90.0) as cliente:
                r = await cliente.post(
                    f"{self.base_url}/inbox/conversations/{conversation_id}/messages",
                    json={"accountId": account_id, "message": texto, "attachmentUrl": url, "attachmentType": "video"},
                    headers=headers,
                )
        except httpx.HTTPError as e:
            logger.error(f"Error de red mandando video a Zernio: {e}")
            return False
        if r.status_code == 200:
            return True
        logger.error(f"Zernio rechazo el video [{r.status_code}]: {r.text[:500]}")
        return False

    async def enviar_documento(
        self, telefono: str, url: str, nombre_archivo: str, texto: str, contexto: dict | None = None
    ) -> bool:
        """Manda un documento (por ejemplo el catalogo en PDF). La URL debe ser publica."""
        contexto = contexto or {}
        conversation_id = contexto.get("conversation_id")
        account_id = contexto.get("account_id") or self.account_id

        api_key = self._api_key(contexto)
        if not api_key or not conversation_id or not account_id:
            logger.error("No se puede enviar el documento: faltan API key, conversation_id o account_id")
            return False

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        evento_id = contexto.get("evento_id")
        if evento_id:
            headers["Idempotency-Key"] = f"agentkit-{evento_id}-doc-{nombre_archivo}"

        try:
            async with httpx.AsyncClient(timeout=60.0) as cliente:
                r = await cliente.post(
                    f"{self.base_url}/inbox/conversations/{conversation_id}/messages",
                    json={
                        "accountId": account_id,
                        "message": texto,
                        "attachmentUrl": url,
                        "attachmentType": "file",
                        "attachmentName": nombre_archivo,
                    },
                    headers=headers,
                )
        except httpx.HTTPError as e:
            logger.error(f"Error de red mandando documento a Zernio: {e}")
            return False

        if r.status_code == 200:
            return True
        logger.error(f"Zernio rechazo el documento [{r.status_code}]: {r.text[:500]}")
        return False

    # ── Diagnostico ──────────────────────────────────────────────────────

    async def verificar_conexion(self) -> tuple[bool, str]:
        """
        Pregunta a Zernio el estado real del numero contra Meta.
        Es la via soportada para saber si el canal esta vivo.
        """
        if not self.api_key:
            return False, "Falta ZERNIO_API_KEY"
        if not self.account_id:
            return True, "ZERNIO_ACCOUNT_ID no configurado: se omite el chequeo del numero"

        try:
            async with httpx.AsyncClient(timeout=15.0) as cliente:
                r = await cliente.get(
                    f"{self.base_url}/whatsapp/number-info",
                    params={"accountId": self.account_id},
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
        except httpx.HTTPError as e:
            return False, f"No se pudo contactar a Zernio: {e}"

        if r.status_code != 200:
            return False, f"Zernio respondio {r.status_code}: {r.text[:200]}"

        telefono = r.json().get("phone") or {}
        return True, (
            f"Numero {telefono.get('display_phone_number', '?')} conectado "
            f"(calidad: {telefono.get('quality_rating', '?')})"
        )
