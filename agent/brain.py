# agent/brain.py — Cerebro del agente: conexion con Claude
# Generado por AgentKit

"""
Logica de IA del agente. Lee el system prompt de config/prompts.yaml y genera las
respuestas con la API de Anthropic.
"""

import logging
import os

import yaml
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

from agent.catalogo import PAQUETES_PUEBLA, VIDEOS_TOY_STORY, ZONAS_PUEBLA, ids_validos

load_dotenv()
logger = logging.getLogger("agentkit")

client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# El modelo se cambia desde .env, sin tocar el codigo.
#   claude-opus-5-5   el mas capaz             $4 / $20 por millon de tokens
#   claude-sonnet-5-5 el balanceado (default)  $2 / $10
#   claude-haiku-5-5  el mas barato y rapido   $0.10 / $0.50
# El "or" y no el default de os.getenv: una variable declarada vacia en el .env
# devuelve "" y dejaria al agente sin modelo.
MODELO = os.getenv("ANTHROPIC_MODEL") or "claude-sonnet-5-5"

# Es un bot de respuestas cortas: con esfuerzo bajo contesta mas rapido y mas barato.
# Dejalo vacio en el .env para no mandar el parametro.
ESFUERZO = os.getenv("ANTHROPIC_EFFORT", "low").strip()

# WhatsApp son mensajes cortos, pero este tope NO es solo la respuesta: en los modelos
# actuales el razonamiento interno tambien cuenta contra el. Con el margen justo, una
# pregunta que exija pensar un poco deja al agente sin espacio para contestar.
MAX_TOKENS = int(os.getenv("ANTHROPIC_MAX_TOKENS") or "4096")

# Los modelos mas viejos no aceptan output_config. Si la primera llamada falla por eso,
# se reintenta sin el parametro y se recuerda para las siguientes.
_soporta_esfuerzo = True

# Herramienta para mandar la foto de un producto del catalogo por WhatsApp.
MAX_FOTOS_POR_RESPUESTA = 6   # tope de fotos sueltas por respuesta (lo aplica main.py)
MAX_VUELTAS_HERRAMIENTAS = 5
HERRAMIENTA_FOTO = {
    "name": "enviar_foto",
    "description": (
        "Envia al cliente por WhatsApp fotos reales de un producto del catalogo, con su nombre "
        "y precio. Por defecto manda solo la foto principal (para mostrar opciones). Con "
        "todas=true manda todas las fotos del producto desde distintos angulos (para cuando el "
        "cliente quiere verlo a detalle o pide mas fotos). "
        f"Maximo {MAX_FOTOS_POR_RESPUESTA} fotos en total por respuesta."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "producto_id": {
                "type": "string",
                "enum": ids_validos(),
                "description": "Id del producto, tal cual aparece entre corchetes en el catalogo.",
            },
            "todas": {
                "type": "boolean",
                "description": "true = mandar todas las fotos del producto; false (default) = solo la principal.",
            },
        },
        "required": ["producto_id"],
    },
}


HERRAMIENTA_CATALOGO = {
    "name": "enviar_catalogo",
    "description": (
        "Envia al cliente por WhatsApp el catalogo completo de juguetes de Kicks Tepeaca en "
        "PDF (con fotos y precios de todos los juguetes). Usala cuando el cliente pida el "
        "catalogo, la lista completa, 'todo lo que tienen' o ver todos los juguetes. "
        "Mandalo una sola vez por conversacion salvo que lo pidan de nuevo."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "tipo": {
                "type": "string",
                "enum": ["paquetes", "juguetes"],
                "description": (
                    "Solo campana de Puebla: 'paquetes' = catalogo de paquetes Toy Story; 'juguetes' = "
                    "catalogo general de juguetes de Kicks (cuando pidan otros juguetes). En Instagram y "
                    "Facebook se manda siempre el de juguetes."
                ),
            }
        },
    },
}


HERRAMIENTA_FOTOS_PAQUETE = {
    "name": "enviar_fotos_paquete",
    "description": (
        "Solo campana de Puebla. Envia al cliente por WhatsApp una foto de cada juguete que "
        "incluye un paquete de Toy Story (sin precios). Usala cuando pregunten por un paquete "
        "o pidan ver fotos de lo que incluye. Mandalas una sola vez por paquete."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "paquete": {"type": "string", "enum": list(PAQUETES_PUEBLA.keys())},
        },
        "required": ["paquete"],
    },
}

HERRAMIENTA_VIDEO = {
    "name": "enviar_video",
    "description": (
        "Solo Puebla. Envia al cliente por WhatsApp un video corto del juguete (para verlo a "
        "detalle). Usalo cuando pidan un video o quieran ver el juguete 'en accion'. Para un "
        "paquete manda los videos de sus juguetes (maximo 3 por respuesta). No mandes videos si "
        "no los piden. Videos disponibles: "
        + "; ".join(f"{k} = {v[1]}" for k, v in VIDEOS_TOY_STORY.items())
        + ". Todos los juguetes Toy Story tienen video."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"video": {"type": "string", "enum": list(VIDEOS_TOY_STORY.keys())}},
        "required": ["video"],
    },
}

HERRAMIENTA_LISTA_ZONAS = {
    "name": "enviar_lista_zonas",
    "description": (
        "Solo Puebla (WhatsApp). Manda al cliente una LISTA para que toque su zona de entrega. "
        "Usala cuando ya sepa que quiere comprar y todavia no sepas su zona (en vez de pedirsela "
        "por texto). La lista ya trae la pregunta: despues escribe una frase muy corta o nada."
    ),
    "input_schema": {"type": "object", "properties": {}},
}

HERRAMIENTA_LISTA_DIAS = {
    "name": "enviar_lista_dias",
    "description": (
        "Solo Puebla (WhatsApp). Manda al cliente una LISTA con los dias de entrega abiertos para "
        "su zona, para que toque el que le acomode. Usala cuando ya sepas su zona y quiera agendar. "
        "La lista ya trae la pregunta: despues escribe una frase muy corta o nada. Cuando el cliente "
        "elija, te llegara su eleccion como mensaje; entonces usa agendar_entrega."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"zona": {"type": "string", "enum": ZONAS_PUEBLA}},
        "required": ["zona"],
    },
}

HERRAMIENTA_LISTA_RUTAS = {
    "name": "enviar_lista_rutas_programadas",
    "description": (
        "Solo Puebla (WhatsApp). Para cuando el cliente quiere UNA SOLA PIEZA suelta (o menos de lo que "
        "da entrega normal): manda una LISTA con los dias y zonas donde YA vamos a pasar porque hay un "
        "pedido grande agendado. Asi se aprovecha la ruta. Si no hay ninguna, lo sabras por el "
        "resultado. La lista ya trae la pregunta: despues escribe una frase muy corta o nada. Cuando el "
        "cliente elija, usa agendar_entrega con pieza_suelta=true."
    ),
    "input_schema": {"type": "object", "properties": {}},
}

HERRAMIENTA_VER_DIAS = {
    "name": "ver_dias_entrega",
    "description": (
        "Solo Puebla. Consulta los proximos dias de entrega disponibles para una zona (con "
        "horario y lugares que quedan). Usala cuando el cliente ya sabe que quiere y le "
        "ofreciste compartirle los dias. NUNCA ofrezcas un dia que no salga aqui."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"zona": {"type": "string", "enum": ZONAS_PUEBLA}},
        "required": ["zona"],
    },
}

HERRAMIENTA_AGENDAR = {
    "name": "agendar_entrega",
    "description": (
        "Solo Puebla. Agenda la entrega del cliente en un dia que salio en ver_dias_entrega. "
        "Quedara por confirmar: el equipo valida la existencia. Necesitas el nombre del "
        "cliente, la zona, la fecha (YYYY-MM-DD, tal cual salio) y el pedido (juguetes o paquete y total)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "fecha": {"type": "string", "description": "YYYY-MM-DD, tal cual salio en ver_dias_entrega."},
            "zona": {"type": "string", "enum": ZONAS_PUEBLA},
            "nombre": {"type": "string"},
            "pedido": {"type": "string", "description": "Que llevara y el total, ej: 'Dúo Woody y Buzz, $1,250'."},
            "pieza_suelta": {
                "type": "boolean",
                "description": "true si es una sola pieza suelta que se lleva aprovechando una ruta ya programada (viene de enviar_lista_rutas_programadas).",
            },
            "preferencia_horario": {
                "type": "string",
                "description": "Opcional: si el horario de su zona no le acomoda, cuando le conviene (ej: 'despues de las 7 PM'). El equipo lo coordina.",
            },
        },
        "required": ["fecha", "zona", "nombre", "pedido"],
    },
}

HERRAMIENTA_EQUIPO = {
    "name": "avisar_equipo",
    "description": (
        "Deja una nota para el equipo. Segun el tipo: 'queja_o_problema' y 'otro' pausan a Rory y "
        "avisan a una persona (usalos solo si de verdad hace falta un humano: queja, problema, "
        "pago, el cliente pide hablar con una persona). 'pedido' avisa que hay un pedido por "
        "confirmar (sin pausar). 'duda_producto' y 'fuera_de_catalogo' solo quedan anotados en el "
        "panel: NO pausan ni molestan, y debes seguir atendiendo al cliente. Despues de usarla, "
        "dile al cliente en una frase lo que corresponda (por ejemplo que lo confirmas mas tarde)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "motivo": {"type": "string", "description": "Resumen corto: que quiere el cliente."},
            "tipo": {
                "type": "string",
                "enum": ["pedido", "duda_producto", "fuera_de_catalogo", "queja_o_problema", "otro"],
                "description": (
                    "pedido = ya eligio producto y dio datos de entrega (nombre/zona); "
                    "duda_producto = pregunta algo del producto que no sabes (medidas, funciones, pilas...); "
                    "fuera_de_catalogo = pide algo que no vendemos en esta campana; "
                    "queja_o_problema = queja, pago, comprobante; otro = lo demas."
                ),
            },
        },
        "required": ["motivo", "tipo"],
    },
}


def cargar_config_prompts() -> dict:
    """Lee toda la configuracion desde config/prompts.yaml."""
    try:
        with open("config/prompts.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/prompts.yaml no encontrado")
        return {}


def cargar_system_prompt() -> str:
    """El system prompt: quien es el agente y que sabe del negocio."""
    return cargar_config_prompts().get(
        "system_prompt", "Eres un asistente util. Responde siempre en espanol."
    )


def obtener_mensaje_error() -> str:
    """Que decirle al cliente cuando algo falla de nuestro lado."""
    return cargar_config_prompts().get(
        "error_message",
        "Lo siento, estoy teniendo problemas tecnicos. Por favor intenta de nuevo en unos minutos.",
    )


def obtener_mensaje_fallback() -> str:
    """Que decirle al cliente cuando no se entendio el mensaje."""
    return cargar_config_prompts().get(
        "fallback_message", "Disculpa, no entendi tu mensaje. Podrias reformularlo?"
    )


def _extraer_texto(respuesta) -> str:
    """
    Junta el texto de la respuesta de Claude.

    Ojo: NO se puede hacer respuesta.content[0].text. La respuesta es una lista de
    bloques y el primero no siempre es texto (los modelos que razonan devuelven
    primero un bloque de pensamiento). Hay que filtrar por tipo.
    """
    partes = [bloque.text for bloque in respuesta.content if bloque.type == "text"]
    return "\n".join(p for p in partes if p).strip()


def _es_error_de_esfuerzo(error: Exception) -> bool:
    """
    True solo si el modelo rechazo la llamada POR el parametro output_config/effort.

    Se exige que sea un 400 de peticion invalida y no cualquier error que mencione la
    palabra: un 529 de sobrecarga que la nombre de paso no debe apagar el parametro
    para todo el proceso.
    """
    if getattr(error, "status_code", None) != 400:
        return False
    texto = str(error).lower()
    return "output_config" in texto or "effort" in texto


async def generar_respuesta(
    mensaje: str,
    historial: list[dict],
    enviar_foto=None,
    enviar_catalogo=None,
    plataforma: str = "whatsapp",
    modo: str = "tepeaca",
    avisar_equipo=None,
    enviar_fotos_paquete=None,
    ver_dias=None,
    agendar=None,
    ejemplos=None,
    enviar_video=None,
    lista_zonas=None,
    lista_dias=None,
    lista_rutas=None,
) -> tuple[str, bool]:
    """
    Genera una respuesta con Claude.

    Args:
        mensaje: el mensaje nuevo del cliente
        historial: los mensajes anteriores, [{"role": "user"|"assistant", "content": "..."}]
        enviar_foto: funcion async opcional, enviar_foto(producto_id) -> str. Si se pasa,
            Claude puede mandar fotos de productos; devuelve el resultado como texto.

    Returns:
        (texto, es_respuesta_real)

        "es_respuesta_real" es False cuando lo que se devuelve es un aviso tecnico
        (error o fallback) y no una respuesta del agente. main.py lo usa para no
        guardar esos avisos en el historial: si se guardaran, quedarian contaminando
        el contexto de todos los mensajes siguientes.
    """
    global _soporta_esfuerzo

    if not mensaje or len(mensaje.strip()) < 2:
        return obtener_mensaje_fallback(), False

    mensajes = [{"role": m["role"], "content": m["content"]} for m in historial]
    mensajes.append({"role": "user", "content": mensaje})

    system_prompt = cargar_system_prompt()
    if modo == "puebla":
        system_prompt += "\n" + cargar_config_prompts().get("puebla_prompt", "")
    if plataforma in ("instagram", "facebook"):
        system_prompt += (
            "\n\n## Canal actual: Instagram (mensajes directos)\n"
            "- El cliente te escribe por Instagram, no por WhatsApp: no tienes su telefono. "
            "Si hay que pasarlo con el equipo o apartar, pidele su nombre y un numero de WhatsApp "
            "o dile que puede escribirnos por WhatsApp al +52 1 223 120 4786 si prefiere.\n"
            "- El catalogo en PDF se manda como enlace; las fotos llegan como imagen y el nombre y "
            "precio en un mensaje aparte.\n"
            "- Mensajes aun mas cortos. Sin markdown.\n"
            "- Nunca menciones que no puedes ver imagenes, anuncios o historias. El cliente "
            "normalmente llega desde un anuncio: simplemente sigue el flujo.\n"
            "- No repitas una pregunta que ya hiciste en el historial; si ya saludaste y "
            "preguntaste por modelo o catalogo, avanza con lo que el cliente pide.\n"
        )
    if avisar_equipo:
        system_prompt += (
            "\n\n## Pasar con una persona\n"
            "Usa la herramienta avisar_equipo cuando el cliente ya decidio comprar o apartar y "
            "dio sus datos, pide hablar con una persona, tiene una queja, quiere colaborar o "
            "preguntas algo que no sabes. Es mejor avisar que inventar.\n"
        )
    if ejemplos:
        bloque = "\n\n## Así responde el dueño (imita su tono, su largo y su forma de cerrar)\n"
        bloque += "Son ejemplos reales de referencia; no los copies literal si el contexto cambia.\n"
        for e in ejemplos:
            bloque += f"\nCliente: {e['cliente']}\nRespuesta del dueño: {e['respuesta']}\n"
            if e.get("nota"):
                bloque += f"(Nota: {e['nota']})\n"
        system_prompt += bloque
    extras = {"output_config": {"effort": ESFUERZO}} if (_soporta_esfuerzo and ESFUERZO) else {}

    async def _llamar(parametros_extra: dict):
        herramientas = []
        if enviar_foto:
            herramientas.append(HERRAMIENTA_FOTO)
        if enviar_catalogo:
            herramientas.append(HERRAMIENTA_CATALOGO)
        if enviar_fotos_paquete:
            herramientas.append(HERRAMIENTA_FOTOS_PAQUETE)
        if enviar_video:
            herramientas.append(HERRAMIENTA_VIDEO)
        if lista_zonas:
            herramientas.append(HERRAMIENTA_LISTA_ZONAS)
        if lista_dias:
            herramientas.append(HERRAMIENTA_LISTA_DIAS)
        if lista_rutas:
            herramientas.append(HERRAMIENTA_LISTA_RUTAS)
        if ver_dias:
            herramientas.append(HERRAMIENTA_VER_DIAS)
        if agendar:
            herramientas.append(HERRAMIENTA_AGENDAR)
        if avisar_equipo:
            herramientas.append(HERRAMIENTA_EQUIPO)
        if herramientas:
            parametros_extra = {**parametros_extra, "tools": herramientas}
        return await client.messages.create(
            model=MODELO,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            messages=mensajes,
            **parametros_extra,
        )

    try:
        respuesta = await _llamar(extras)
    except Exception as e:  # noqa: BLE001
        if extras and _es_error_de_esfuerzo(e):
            logger.warning(
                f"El modelo {MODELO} no acepta output_config.effort; se reintenta sin ese parametro."
            )
            _soporta_esfuerzo = False
            try:
                respuesta = await _llamar({})
            except Exception as e2:  # noqa: BLE001
                logger.error(f"Error llamando a Claude: {e2}")
                return obtener_mensaje_error(), False
        else:
            logger.error(f"Error llamando a Claude: {e}")
            return obtener_mensaje_error(), False

    # Ciclo de herramientas: si Claude pide mandar una foto, la mandamos y le contamos
    # el resultado para que redacte la respuesta final.
    fotos_enviadas = 0
    listas_programadas = 0
    for _ in range(MAX_VUELTAS_HERRAMIENTAS):
        if getattr(respuesta, "stop_reason", None) != "tool_use":
            break

        mensajes.append({"role": "assistant", "content": respuesta.content})
        resultados = []
        for bloque in respuesta.content:
            if bloque.type != "tool_use":
                continue
            if bloque.name == "enviar_foto" and enviar_foto:
                datos = bloque.input or {}
                resultado = await enviar_foto(
                    datos.get("producto_id", ""), bool(datos.get("todas", False))
                )
                if resultado.startswith("OK"):
                    fotos_enviadas += 1
            elif bloque.name == "enviar_catalogo" and enviar_catalogo:
                resultado = await enviar_catalogo((bloque.input or {}).get("tipo", ""))
                if resultado.startswith("OK"):
                    fotos_enviadas += 1
            elif bloque.name == "enviar_fotos_paquete" and enviar_fotos_paquete:
                resultado = await enviar_fotos_paquete((bloque.input or {}).get("paquete", ""))
                if resultado.startswith("OK"):
                    fotos_enviadas += 1
            elif bloque.name == "enviar_video" and enviar_video:
                resultado = await enviar_video((bloque.input or {}).get("video", ""))
                if resultado.startswith("OK"):
                    fotos_enviadas += 1
            elif bloque.name == "enviar_lista_zonas" and lista_zonas:
                resultado = await lista_zonas()
                if resultado.startswith("OK"):
                    listas_programadas += 1
            elif bloque.name == "enviar_lista_rutas_programadas" and lista_rutas:
                resultado = await lista_rutas()
                if resultado.startswith("OK"):
                    listas_programadas += 1
            elif bloque.name == "enviar_lista_dias" and lista_dias:
                resultado = await lista_dias((bloque.input or {}).get("zona", ""))
                if resultado.startswith("OK"):
                    listas_programadas += 1
            elif bloque.name == "ver_dias_entrega" and ver_dias:
                resultado = await ver_dias((bloque.input or {}).get("zona", ""))
            elif bloque.name == "agendar_entrega" and agendar:
                d = bloque.input or {}
                resultado = await agendar(
                    d.get("fecha", ""), d.get("zona", ""), d.get("nombre", ""), d.get("pedido", ""),
                    d.get("preferencia_horario", ""), bool(d.get("pieza_suelta", False)),
                )
            elif bloque.name == "avisar_equipo" and avisar_equipo:
                resultado = await avisar_equipo(
                    (bloque.input or {}).get("motivo", ""), (bloque.input or {}).get("tipo", "otro")
                )
            else:
                resultado = "Herramienta no disponible."
            resultados.append(
                {"type": "tool_result", "tool_use_id": bloque.id, "content": resultado}
            )
        mensajes.append({"role": "user", "content": resultados})

        try:
            respuesta = await _llamar({} if not _soporta_esfuerzo else extras)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error llamando a Claude despues de una herramienta: {e}")
            return obtener_mensaje_error(), False

    if getattr(respuesta, "stop_reason", None) == "max_tokens":
        logger.warning(
            f"La respuesta se corto por llegar al tope de {MAX_TOKENS} tokens. "
            "Si pasa seguido, sube ANTHROPIC_MAX_TOKENS o acorta el system prompt."
        )

    texto = _extraer_texto(respuesta)
    if not texto and listas_programadas:
        return "", True  # la lista ya trae la pregunta: no hace falta mas texto
    if not texto and fotos_enviadas:
        texto = "Ahi te van las fotos. ¿Cual te late mas?"
    if not texto:
        logger.warning("Claude devolvio una respuesta sin texto")
        return obtener_mensaje_fallback(), False

    logger.info(
        f"Respuesta generada con {MODELO} "
        f"({respuesta.usage.input_tokens} in / {respuesta.usage.output_tokens} out)"
    )
    return texto, True
