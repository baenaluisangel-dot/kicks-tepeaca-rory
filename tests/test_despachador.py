# tests/test_despachador.py — La cola de envio y el despachador

"""
Lo que de verdad hay que proteger aqui no es que mande, sino que NO mande cuando
no debe: fuera de la ventana de 24 h, dos veces el mismo mensaje, o encima de un
cliente que ya contesto.
"""

import os
import tempfile
from datetime import timedelta

import pytest

_BASE = os.path.join(tempfile.mkdtemp(), "despacho.db")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_BASE}"
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test")

from sqlalchemy import delete  # noqa: E402

from agent.despachador import VENTANA_HORAS, despachar_una_vuelta  # noqa: E402
from agent.memory import (  # noqa: E402
    MAX_INTENTOS,
    Atencion,
    Envio,
    ahora,
    async_session,
    cancelar_envios_de,
    contexto_de,
    encolar_envio,
    envios_por_salir,
    horas_desde_su_ultimo_mensaje,
    inicializar_db,
    marcar_envio,
    registrar_mensaje_cliente,
    resumen_cola,
    tomar_envio,
)

CTX = {"conversation_id": "conv-1", "account_id": "acc-1"}


class ProveedorFalso:
    """Anota lo que se le pidio mandar, en vez de mandarlo."""

    def __init__(self, funciona=True):
        self.funciona = funciona
        self.mandados: list[tuple[str, str]] = []

    async def enviar_mensaje(self, telefono, mensaje, contexto=None):
        self.mandados.append((telefono, mensaje))
        return self.funciona

    async def enviar_video(self, telefono, url, pie="", contexto=None):
        self.mandados.append((telefono, f"[video] {url}"))
        return self.funciona


@pytest.fixture(autouse=True)
async def base_limpia():
    await inicializar_db()
    async with async_session() as sesion:
        await sesion.execute(delete(Envio))
        await sesion.execute(delete(Atencion))
        await sesion.commit()


async def _cliente_que_acaba_de_escribir(telefono="2221234567"):
    await registrar_mensaje_cliente(telefono, "whatsapp", "hola", CTX)
    return telefono


async def _envejecer(telefono: str, horas: float):
    """Mueve hacia atras el ultimo mensaje del cliente, para salir de la ventana."""
    async with async_session() as sesion:
        fila = await sesion.get(Atencion, telefono)
        fila.ultimo_en = ahora() - timedelta(hours=horas)
        await sesion.commit()


@pytest.mark.asyncio
async def test_dentro_de_la_ventana_si_manda():
    tel = await _cliente_que_acaba_de_escribir()
    await encolar_envio(tel, "¿Te late el sábado en Peri Plaza?", motivo="seguimiento 20h")

    proveedor = ProveedorFalso()
    conteo = await despachar_una_vuelta(proveedor)

    assert conteo["enviados"] == 1
    assert proveedor.mandados == [(tel, "¿Te late el sábado en Peri Plaza?")]
    assert (await resumen_cola()).get("enviado") == 1


@pytest.mark.asyncio
async def test_fuera_de_la_ventana_no_manda_nada():
    """El caso que importa: mandar aqui falla en Meta y quema la calidad del numero."""
    tel = await _cliente_que_acaba_de_escribir()
    await _envejecer(tel, VENTANA_HORAS + 1)
    await encolar_envio(tel, "Sigo al pendiente")

    proveedor = ProveedorFalso()
    conteo = await despachar_una_vuelta(proveedor)

    assert conteo["sin_ventana"] == 1
    assert proveedor.mandados == [], "no debe salir nada fuera de la ventana"
    assert (await resumen_cola()).get("requiere_plantilla") == 1


@pytest.mark.asyncio
async def test_sin_contexto_guardado_no_se_puede_mandar():
    """Zernio necesita conversation_id; sin el, el telefono solo no alcanza."""
    await encolar_envio("2229999999", "hola")
    conteo = await despachar_una_vuelta(ProveedorFalso())
    assert conteo["sin_ventana"] == 1, "nunca escribio: ni siquiera hay ventana"


@pytest.mark.asyncio
async def test_dos_despachadores_no_lo_mandan_dos_veces():
    tel = await _cliente_que_acaba_de_escribir()
    envio_id = await encolar_envio(tel, "una sola vez")

    assert await tomar_envio(envio_id) is True
    assert await tomar_envio(envio_id) is False, "el segundo no debe llevarselo"


@pytest.mark.asyncio
async def test_un_fallo_se_reintenta_y_despues_se_rinde():
    tel = await _cliente_que_acaba_de_escribir()
    await encolar_envio(tel, "se va a caer")

    proveedor = ProveedorFalso(funciona=False)
    for _ in range(MAX_INTENTOS):
        # El reintento se programa a futuro: se adelanta para no esperar en la prueba
        async with async_session() as sesion:
            for f in (await sesion.execute(__import__("sqlalchemy").select(Envio))).scalars():
                f.programado_para = ahora()
            await sesion.commit()
        await despachar_una_vuelta(proveedor)

    assert (await resumen_cola()).get("fallido") == 1
    assert await envios_por_salir() == [], "ya no se reintenta mas"


@pytest.mark.asyncio
async def test_si_el_cliente_contesta_se_cancela_lo_programado():
    """Insistirle encima de su mensaje queda pesimo."""
    tel = await _cliente_que_acaba_de_escribir()
    await encolar_envio(tel, "primer recordatorio")
    await encolar_envio(tel, "segundo recordatorio")

    assert await cancelar_envios_de(tel) == 2
    assert await envios_por_salir() == []
    assert (await resumen_cola()).get("cancelado") == 2


@pytest.mark.asyncio
async def test_lo_programado_a_futuro_todavia_no_sale():
    tel = await _cliente_que_acaba_de_escribir()
    await encolar_envio(tel, "mañana", demora_horas=20)
    assert await envios_por_salir() == []


@pytest.mark.asyncio
async def test_el_contexto_se_guarda_y_se_recupera():
    tel = await _cliente_que_acaba_de_escribir()
    assert (await contexto_de(tel))["conversation_id"] == "conv-1"
    assert await horas_desde_su_ultimo_mensaje(tel) < 0.1
    assert await horas_desde_su_ultimo_mensaje("2220000000") is None
