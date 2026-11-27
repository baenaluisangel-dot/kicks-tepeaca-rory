# tests/test_mercado_resumen.py — El resumen de lo que vio el scraper

import pytest

from sqlalchemy import delete

from agent.memory import (
    Observacion,
    async_session,
    guardar_observaciones,
    inicializar_db,
    resumen_mercado,
)


def obs(**kw):
    base = {"fuente": "tiktok", "url": f"https://t.co/{kw.get('url_n', 1)}",
            "autor": "@mayorista_mx", "texto": "oferta", "producto": "Rex",
            "precio": 300.0, "moneda": "MXN", "tipo_precio": "mayoreo",
            "proposito": "evaluar"}
    base.update({k: v for k, v in kw.items() if k != "url_n"})
    return base


@pytest.fixture(autouse=True)
async def db():
    """Cada prueba arranca con la tabla vacia: si no, se contaminan entre si."""
    await inicializar_db()
    async with async_session() as session:
        await session.execute(delete(Observacion))
        await session.commit()
    yield


async def test_sin_observaciones_no_truena():
    r = await resumen_mercado()
    assert r["total"] == 0 and r["productos"] == []


async def test_agrupa_por_producto_y_tipo():
    await guardar_observaciones([
        obs(url="https://t.co/1", precio=300.0, tipo_precio="mayoreo"),
        obs(url="https://t.co/2", precio=380.0, tipo_precio="mayoreo"),
        obs(url="https://t.co/3", precio=520.0, tipo_precio="menudeo"),
    ])
    r = await resumen_mercado()
    assert r["total"] == 3 and r["con_precio"] == 3
    rex = next(p for p in r["productos"] if p["nombre"] == "Rex")
    assert rex["tipos"]["mayoreo"]["n"] == 2
    assert rex["tipos"]["mayoreo"]["min"] == 300 and rex["tipos"]["mayoreo"]["max"] == 380
    assert rex["tipos"]["menudeo"]["n"] == 1


async def test_la_mediana_no_la_mueve_un_precio_raro():
    """Tres precios normales y uno absurdo: la mediana aguanta, el promedio no."""
    await guardar_observaciones([
        obs(url="https://t.co/a", precio=300.0),
        obs(url="https://t.co/b", precio=320.0),
        obs(url="https://t.co/c", precio=340.0),
        obs(url="https://t.co/d", precio=99000.0),
    ])
    rex = (await resumen_mercado())["productos"][0]
    assert rex["tipos"]["mayoreo"]["mediana"] == 340
    assert sum([300, 320, 340, 99000]) / 4 > 24000  # el promedio seria basura


async def test_cuenta_los_que_no_traen_precio():
    await guardar_observaciones([
        obs(url="https://t.co/x", precio=0.0),
        obs(url="https://t.co/y", precio=300.0),
    ])
    r = await resumen_mercado()
    assert r["sin_precio"] == 1 and r["con_precio"] == 1


async def test_producto_vacio_no_desaparece():
    await guardar_observaciones([obs(url="https://t.co/z", producto="", precio=250.0)])
    assert (await resumen_mercado())["productos"][0]["nombre"] == "(sin producto)"


async def test_acentos_y_mayusculas_son_el_mismo_producto():
    await guardar_observaciones([
        obs(url="https://t.co/m1", producto="Camión", precio=200.0),
        obs(url="https://t.co/m2", producto="camion", precio=240.0),
    ])
    r = await resumen_mercado()
    assert len(r["productos"]) == 1 and r["productos"][0]["tipos"]["mayoreo"]["n"] == 2


async def test_lista_las_cuentas_mas_frecuentes():
    await guardar_observaciones([
        obs(url="https://t.co/p1", autor="@uno"),
        obs(url="https://t.co/p2", autor="@uno"),
        obs(url="https://t.co/p3", autor="@dos"),
    ])
    assert (await resumen_mercado())["autores"][0] == ("@uno", 2)
