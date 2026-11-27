# tests/test_mercado.py — El endpoint por donde entra lo del scraper

"""
El endpoint esta abierto a internet, asi que lo que importa probar es que no
acepte nada que no deba: sin token, con token de otro, lotes gigantes, filas
sin url, o el mismo video dos veces.
"""

import os
import tempfile

import pytest

_BASE = os.path.join(tempfile.mkdtemp(), "mercado.db")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_BASE}"
os.environ["SCRAPER_TOKEN"] = "token-de-prueba"
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import delete  # noqa: E402

from agent.memory import (  # noqa: E402
    Observacion,
    async_session,
    guardar_observaciones,
    inicializar_db,
    precios_observados,
)
from agent.mercado import router  # noqa: E402

app = FastAPI()
app.include_router(router)
cliente = TestClient(app)

AUTH = {"Authorization": "Bearer token-de-prueba"}


def observacion(url, precio=850, tipo="menudeo", producto="Tiro al Blanco"):
    return {"fuente": "tiktok", "url": url, "autor": "@jugueteria",
            "texto": "Tiro al Blanco original, entrega en Puebla",
            "producto": producto, "precio": precio, "tipo_precio": tipo,
            "vistas": 12000, "proposito": "evaluar"}


@pytest.fixture(autouse=True)
async def base_limpia():
    await inicializar_db()
    async with async_session() as sesion:
        await sesion.execute(delete(Observacion))
        await sesion.commit()


def test_sin_token_no_entra():
    r = cliente.post("/observaciones", json=[observacion("https://tiktok.com/v/1")])
    assert r.status_code == 401


def test_con_token_de_otro_tampoco():
    r = cliente.post("/observaciones", json=[observacion("https://tiktok.com/v/1")],
                     headers={"Authorization": "Bearer el-token-de-alguien-mas"})
    assert r.status_code == 401


def test_un_lote_normal_entra():
    r = cliente.post("/observaciones", headers=AUTH, json=[
        observacion("https://tiktok.com/v/1"),
        observacion("https://tiktok.com/v/2", precio=900),
    ])
    assert r.status_code == 200
    assert r.json()["guardadas"] == 2


def test_el_mismo_video_no_se_duplica():
    """El scraper puede pasar dos veces por el mismo video; no debe contar doble."""
    cliente.post("/observaciones", headers=AUTH, json=[observacion("https://tiktok.com/v/3")])
    r = cliente.post("/observaciones", headers=AUTH, json=[observacion("https://tiktok.com/v/3")])
    assert r.json() == {"guardadas": 0, "repetidas": 1, "rechazadas": 0, "motivos": []}


def test_una_fila_sin_url_se_rechaza_sin_tumbar_el_lote():
    """La url es la huella del dato: sin ella no se puede verificar ni deduplicar."""
    r = cliente.post("/observaciones", headers=AUTH, json=[
        observacion("https://tiktok.com/v/4"),
        {"fuente": "tiktok", "producto": "algo", "precio": 100},   # sin url
    ])
    datos = r.json()
    assert datos["guardadas"] == 1 and datos["rechazadas"] == 1
    assert "sin url valida" in datos["motivos"][0]


def test_un_lote_gigante_se_rechaza():
    r = cliente.post("/observaciones", headers=AUTH,
                     json=[observacion(f"https://tiktok.com/v/g{i}") for i in range(501)])
    assert r.status_code == 413


@pytest.mark.asyncio
async def test_un_tipo_de_precio_inventado_cae_en_no_claro():
    """Mejor admitir que no se sabe que inventar que un precio es de mayoreo."""
    await guardar_observaciones([observacion("https://tiktok.com/v/5", tipo="rebajado")])
    async with async_session() as sesion:
        fila = (await sesion.execute(__import__("sqlalchemy").select(Observacion))).scalars().first()
    assert fila.tipo_precio == "no_claro"


@pytest.mark.asyncio
async def test_los_precios_devuelven_rango_y_no_solo_promedio():
    """Con pocas observaciones un promedio solo engaña mas de lo que informa."""
    await guardar_observaciones([
        observacion("https://tiktok.com/v/a", precio=800),
        observacion("https://tiktok.com/v/b", precio=850),
        observacion("https://tiktok.com/v/c", precio=1200),
        observacion("https://tiktok.com/v/d", precio=600, tipo="mayoreo"),
    ])
    r = await precios_observados("Tiro al Blanco", "menudeo")
    assert r["n"] == 3, "el de mayoreo no se mezcla con los de menudeo"
    assert (r["min"], r["mediana"], r["max"]) == (800, 850, 1200)
    assert len(r["fuentes"]) == 3, "cada precio trae de donde salio"


@pytest.mark.asyncio
async def test_sin_observaciones_no_inventa_un_precio():
    assert await precios_observados("Algo que nadie vende") == {"n": 0}


# ── Texto que Postgres no puede tragar ───────────────────────────────────────

async def test_un_emoji_partido_no_tumba_la_observacion():
    """
    Los captions de TikTok llegan con surrogates sueltos. Antes reventaban la
    insercion, el endpoint devolvia 500 y el scraper reintentaba el mismo
    veneno para siempre.
    """
    from agent.memory import guardar_observaciones

    r = await guardar_observaciones([{
        "fuente": "tiktok", "url": "https://t.co/emoji-roto",
        "texto": "Juguetes BARATOS al mayoreo \ud83e en el centro",
        "producto": "Woody \ud83e", "autor": "@tienda\ud83e",
        "precio": 300, "tipo_precio": "mayoreo",
    }])
    assert r["guardadas"] == 1, r

    from agent.memory import observaciones
    fila = next(o for o in await observaciones(limite=500)
                if o["url"] == "https://t.co/emoji-roto")
    assert "\ud83e" not in fila["texto"]
    assert "BARATOS al mayoreo" in fila["texto"]   # lo demas sobrevive intacto


async def test_una_fila_mala_no_se_lleva_el_lote():
    """La buena entra aunque la de junto falle: perder el lote es perder horas."""
    from agent.memory import guardar_observaciones

    r = await guardar_observaciones([
        {"fuente": "tiktok", "url": "https://t.co/lote-ok-1",
         "texto": "normal", "producto": "Rex", "precio": 300},
        {"fuente": "tiktok", "url": "no-es-una-url", "texto": "mala"},
        {"fuente": "tiktok", "url": "https://t.co/lote-ok-2",
         "texto": "normal", "producto": "Buzz", "precio": 400},
    ])
    assert r["guardadas"] == 2 and r["rechazadas"] == 1
