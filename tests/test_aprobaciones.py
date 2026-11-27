# tests/test_aprobaciones.py — Cola de aprobaciones y bitacora

"""
Prueba el camino completo: se encola una decision, Luisito toca un boton y la
espera queda resuelta. Cubre los dos casos que de verdad importan: que un segundo
toque no la resuelva dos veces, y que un chat ajeno no pueda decidir nada.

No toca Telegram: solo la base y la verificacion del chat.
"""

import os
import tempfile

import pytest

# memory.py lee DATABASE_URL al importarse, asi que se fija antes.
_BASE = os.path.join(tempfile.mkdtemp(), "prueba.db")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_BASE}"
os.environ["TELEGRAM_CHAT_ID"] = "123456"

from sqlalchemy import delete  # noqa: E402

from agent.memory import (  # noqa: E402
    Bitacora,
    Espera,
    async_session,
    crear_espera,
    inicializar_db,
    listar_bitacora,
    listar_esperas,
    registrar_bitacora,
    resolver_espera,
)
from agent.notificar import chat_autorizado  # noqa: E402


@pytest.fixture(autouse=True)
async def base_lista():
    """Base limpia antes de cada prueba: comparten archivo y se contaminarian."""
    await inicializar_db()
    async with async_session() as sesion:
        await sesion.execute(delete(Espera))
        await sesion.execute(delete(Bitacora))
        await sesion.commit()


@pytest.mark.asyncio
async def test_la_cola_ordena_por_lo_que_cuesta_esperar():
    """Lo viejo no siempre es lo que mas cuesta: manda el dinero detenido."""
    await crear_espera("cierre", "Barata pero vieja", costo_esperar=350)
    await crear_espera("cierre", "Cara y reciente", costo_esperar=3430)

    cola = await listar_esperas()
    assert [e["asunto"] for e in cola] == ["Cara y reciente", "Barata pero vieja"]


@pytest.mark.asyncio
async def test_un_segundo_toque_no_la_resuelve_dos_veces():
    """Telegram reintenta y el dedo resbala: la segunda vez no debe hacer nada."""
    espera_id = await crear_espera(
        "pauta", "¿Subo el presupuesto a $400?",
        opciones=[{"clave": "si", "etiqueta": "Sí"}, {"clave": "no", "etiqueta": "No"}],
        costo_esperar=1200,
    )

    primera = await resolver_espera(espera_id, "si")
    assert primera is not None and primera["respuesta"] == "si"

    segunda = await resolver_espera(espera_id, "no")
    assert segunda is None, "una espera ya contestada no se vuelve a resolver"

    assert await listar_esperas(estado="pendiente") == []
    resueltas = await listar_esperas(estado="resuelta")
    assert resueltas[0]["respuesta"] == "si", "la primera respuesta es la que vale"


@pytest.mark.asyncio
async def test_resolver_una_espera_que_no_existe():
    assert await resolver_espera(99999, "si") is None


@pytest.mark.asyncio
async def test_solo_el_chat_de_luisito_decide():
    """La unica barrera entre un boton que aprueba y cualquiera que le escriba al bot."""
    assert chat_autorizado("123456") is True
    assert chat_autorizado(123456) is True, "Telegram manda el id como numero"
    assert chat_autorizado("999999") is False
    assert chat_autorizado("") is False
    assert chat_autorizado(None) is False


@pytest.mark.asyncio
async def test_sin_chat_configurado_no_autoriza_a_nadie():
    """Si falta la variable, el default seguro es rechazar, no aceptar a todos."""
    previo = os.environ.pop("TELEGRAM_CHAT_ID", "")
    try:
        assert chat_autorizado("123456") is False
    finally:
        os.environ["TELEGRAM_CHAT_ID"] = previo


@pytest.mark.asyncio
async def test_la_bitacora_guarda_tambien_lo_que_no_paso():
    """Un agente que no hizo nada tambien reporta: el silencio parece una falla."""
    await registrar_bitacora("organico", "sin cambios", "no hubo dudas nuevas", "ok")
    await registrar_bitacora("cierre", "3 seguimientos encolados", resultado="escalado")

    filas = await listar_bitacora()
    assert len(filas) == 2
    assert filas[0]["agente"] == "cierre", "lo mas reciente primero"

    solo_organico = await listar_bitacora(agente="organico")
    assert len(solo_organico) == 1 and solo_organico[0]["accion"] == "sin cambios"
