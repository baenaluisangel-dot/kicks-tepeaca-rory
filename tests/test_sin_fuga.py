# tests/test_sin_fuga.py — Que el token del bot no acabe en los logs

"""
Telegram mete el token del bot DENTRO de la ruta de cada peticion:
  POST https://api.telegram.org/bot<TOKEN>/sendMessage

httpx registra esa URL completa a nivel INFO, asi que durante semanas el token
estuvo en texto plano en los logs de Railway. Esta prueba existe para que nadie
vuelva a subir el nivel de httpx sin darse cuenta de lo que destapa.
"""

import logging
import os

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./prueba_fuga.db")


def test_httpx_no_registra_urls():
    """Importar main.py tiene que dejar httpx callado por debajo de WARNING."""
    import agent.main  # noqa: F401  — el efecto de importarlo es lo que se prueba

    for ruidosa in ("httpx", "httpcore"):
        nivel = logging.getLogger(ruidosa).level
        assert nivel >= logging.WARNING, (
            f"{ruidosa} esta en {logging.getLevelName(nivel)}: a INFO registra la URL "
            "completa de cada peticion, y la de Telegram lleva el token del bot dentro."
        )
