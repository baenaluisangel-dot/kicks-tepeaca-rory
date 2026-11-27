# agent/despachador.py — Manda lo que el agente de Cierre dejo en la cola

"""
El agente de Cierre decide a quien escribirle y lo deja en cola_envio. Este modulo
lo manda.

Estan separados a proposito. El agente corre por lotes en la nube, cada varias horas;
el despachador vive en Railway y mira la cola seguido, porque si se puede mandar o no
depende de la ventana de 24 horas de WhatsApp, y eso cambia entre que se decidio el
mensaje y que toca mandarlo.

Lo que NO hace: mandar fuera de la ventana. Ahi hace falta una plantilla aprobada por
Meta, asi que el envio se marca como requiere_plantilla y se queda esperando en vez
de salir y fallar.
"""

import asyncio
import logging
import os

from agent.memory import (
    contexto_de,
    envios_por_salir,
    horas_desde_su_ultimo_mensaje,
    marcar_envio,
    registrar_bitacora,
    resumen_cola,
    tomar_envio,
)

logger = logging.getLogger("agentkit")

# WhatsApp deja mandar texto libre dentro de las 24 h del ultimo mensaje del cliente.
# Se corta en 23.5 para no quedar justo en el filo: entre que se decide y que sale el
# mensaje pasan segundos, y Meta cuenta desde su reloj, no desde el nuestro.
VENTANA_HORAS = 23.5

INTERVALO_SEGUNDOS = int(os.getenv("DESPACHADOR_INTERVALO") or "300")   # 5 minutos


async def _mandar(proveedor, envio: dict, contexto: dict) -> tuple[bool, str]:
    """Manda el adjunto si lo hay, y luego el texto. Devuelve (salio, detalle)."""
    tipo, url = envio.get("adjunto_tipo"), envio.get("adjunto_url")
    if tipo and url:
        envios = {
            "video": getattr(proveedor, "enviar_video", None),
            "audio": getattr(proveedor, "enviar_audio", None),
            "foto": getattr(proveedor, "enviar_imagen", None),
        }
        funcion = envios.get(tipo)
        if funcion is None:
            return False, f"el proveedor no sabe mandar '{tipo}'"
        try:
            if tipo == "audio":
                ok = await funcion(envio["telefono"], url, contexto)
            else:
                ok = await funcion(envio["telefono"], url, "", contexto)
        except Exception as e:  # noqa: BLE001
            return False, f"fallo el adjunto: {type(e).__name__}"
        if not ok:
            return False, "el proveedor rechazo el adjunto"

    if not envio.get("texto"):
        return True, "solo adjunto"

    try:
        ok = await proveedor.enviar_mensaje(envio["telefono"], envio["texto"], contexto)
    except Exception as e:  # noqa: BLE001
        return False, f"fallo el texto: {type(e).__name__}"
    return (True, "") if ok else (False, "el proveedor rechazo el texto")


async def despachar_una_vuelta(proveedor) -> dict:
    """
    Manda lo que toque ahora. Devuelve el conteo por resultado.

    Se puede llamar sola desde una prueba o desde el panel, sin esperar al bucle.
    """
    conteo = {"enviados": 0, "sin_ventana": 0, "sin_contexto": 0, "fallidos": 0}
    if proveedor is None:
        return conteo

    for envio in await envios_por_salir():
        # Apartarlo primero: si dos despachadores corren a la vez, solo uno se lo lleva
        if not await tomar_envio(envio["id"]):
            continue

        horas = await horas_desde_su_ultimo_mensaje(envio["telefono"])
        if horas is None or horas > VENTANA_HORAS:
            await marcar_envio(
                envio["id"], "requiere_plantilla",
                f"el cliente escribio hace {horas:.0f} h" if horas else "nunca escribio",
            )
            conteo["sin_ventana"] += 1
            continue

        contexto = await contexto_de(envio["telefono"])
        if not contexto.get("conversation_id"):
            await marcar_envio(envio["id"], "fallido", "no hay contexto guardado de ese cliente")
            conteo["sin_contexto"] += 1
            continue

        salio, detalle = await _mandar(proveedor, envio, contexto)
        if salio:
            await marcar_envio(envio["id"], "enviado", detalle)
            conteo["enviados"] += 1
            logger.info(f"Despachado a {envio['telefono']}: {envio.get('motivo') or 'sin motivo'}")
        else:
            await marcar_envio(envio["id"], "fallido", detalle)
            conteo["fallidos"] += 1
            logger.warning(f"No se pudo despachar a {envio['telefono']}: {detalle}")

    return conteo


async def bucle_despachador(obtener_proveedor):
    """
    Revisa la cola cada pocos minutos, para siempre.

    Recibe una funcion y no el proveedor directo, porque al arrancar puede no estar
    configurado todavia y no queremos que el bucle se quede con un None para siempre.
    """
    logger.info(f"Despachador en marcha, cada {INTERVALO_SEGUNDOS} s")
    while True:
        await asyncio.sleep(INTERVALO_SEGUNDOS)
        try:
            conteo = await despachar_una_vuelta(obtener_proveedor())
            if any(conteo.values()):
                logger.info(f"Despachador: {conteo}")
                # Un agente que no hizo nada tambien reporta, pero una vuelta vacia
                # cada cinco minutos llenaria la bitacora de ruido.
                await registrar_bitacora(
                    "despachador",
                    f"{conteo['enviados']} enviados, {conteo['sin_ventana']} sin ventana",
                    resultado="escalado" if conteo["sin_ventana"] or conteo["fallidos"] else "ok",
                )
        except Exception as e:  # noqa: BLE001 — una vuelta mala no debe matar el bucle
            logger.exception(f"El despachador fallo en una vuelta: {e}")


async def pendientes_por_plantilla() -> int:
    """Cuantos envios estan esperando una plantilla aprobada. Para el resumen diario."""
    return (await resumen_cola()).get("requiere_plantilla", 0)
