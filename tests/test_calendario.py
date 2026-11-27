# tests/test_calendario.py — La fecha prioritaria que Rory ofrece primero

import datetime as dt
import os

import pytest

import agent.calendario as cal

HOY = cal.ahora_mx().date()
MANANA = (HOY + dt.timedelta(days=1)).isoformat()
PASADO = (HOY + dt.timedelta(days=2)).isoformat()


@pytest.fixture(autouse=True)
def ruta_falsa(monkeypatch):
    """Una sola parada (CAPU) con lugar manana y pasado manana."""

    async def dias(zona, desde):
        return [{"fecha": MANANA, "horario": "13:00 a 14:00"},
                {"fecha": PASADO, "horario": "13:00 a 14:00"}]

    async def cfg():
        return {"paradas": [{"zona": "CAPU", "inicio": "13:00", "fin": "14:00",
                             "cupo": 3, "punto": "afuera de la terminal"}]}

    monkeypatch.setattr(cal, "dias_disponibles", dias)
    monkeypatch.setattr(cal, "obtener_config_ruta", cfg)
    monkeypatch.delenv("EMPUJAR_FECHA", raising=False)


async def test_sin_variable_no_empuja_nada():
    assert "Fecha prioritaria" not in await cal.texto_calendario("puebla")


async def test_empuja_manana_con_su_zona(monkeypatch):
    monkeypatch.setenv("EMPUJAR_FECHA", MANANA)
    texto = await cal.texto_calendario("puebla")
    assert "Fecha prioritaria" in texto
    assert "MAÑANA" in texto and "CAPU" in texto


async def test_una_fecha_sin_cupo_no_se_empuja(monkeypatch):
    """Si ese dia no tiene lugar, Rory no debe ofrecerlo: prometeria algo que no existe."""
    monkeypatch.setenv("EMPUJAR_FECHA", (HOY + dt.timedelta(days=30)).isoformat())
    assert "Fecha prioritaria" not in await cal.texto_calendario("puebla")


async def test_una_fecha_invalida_no_truena(monkeypatch):
    monkeypatch.setenv("EMPUJAR_FECHA", "el sabado")
    assert "Fecha prioritaria" not in await cal.texto_calendario("puebla")


async def test_el_modo_tepeaca_no_lleva_calendario_de_ruta(monkeypatch):
    monkeypatch.setenv("EMPUJAR_FECHA", MANANA)
    assert "Fecha prioritaria" not in await cal.texto_calendario("tepeaca")
