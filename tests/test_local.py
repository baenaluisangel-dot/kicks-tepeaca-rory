# tests/test_local.py — Simulador de chat en terminal
# Generado por AgentKit

"""
Prueba tu agente sin necesitar WhatsApp.
Simula una conversacion en la terminal.
"""

import asyncio
import os
import sys

# Agregar el directorio raiz al path para poder importar "agent"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.brain import generar_respuesta  # noqa: E402
from agent.catalogo import obtener_producto, pie_de_foto  # noqa: E402
from agent.memory import (  # noqa: E402
    guardar_mensaje,
    inicializar_db,
    limpiar_historial,
    obtener_historial,
)

TELEFONO_TEST = "test-local-001"


async def main():
    """Loop principal del chat de prueba."""
    await inicializar_db()

    print()
    print("=" * 55)
    print("   AgentKit — Test Local")
    print("=" * 55)
    print()
    print("  Escribe mensajes como si fueras un cliente.")
    print("  Comandos especiales:")
    print("    'limpiar'  — borra el historial")
    print("    'salir'    — termina el test")
    print()
    print("-" * 55)
    print()

    while True:
        try:
            mensaje = input("Tu: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\nTest finalizado.")
            break

        if not mensaje:
            continue

        if mensaje.lower() == "salir":
            print("\nTest finalizado.")
            break

        if mensaje.lower() == "limpiar":
            await limpiar_historial(TELEFONO_TEST)
            print("[Historial borrado]\n")
            continue

        # El historial se lee ANTES de guardar (brain.py agrega el mensaje actual)
        historial = await obtener_historial(TELEFONO_TEST)

        fotos_mandadas: list[str] = []

        async def enviar_foto_simulada(producto_id: str) -> str:
            producto = obtener_producto(producto_id)
            if not producto:
                return f"ERROR: no existe el producto '{producto_id}'."
            print(f"\n  [FOTO] {pie_de_foto(producto)}  ({producto['foto']})")
            fotos_mandadas.append(producto["nombre"])
            return f"OK: foto de {producto['nombre']} enviada."

        print("\nAgente: ", end="", flush=True)
        respuesta, es_respuesta_real = await generar_respuesta(
            mensaje, historial, enviar_foto_simulada
        )
        print(respuesta)
        print()

        # Igual que en produccion: los avisos tecnicos no entran al historial
        if es_respuesta_real:
            await guardar_mensaje(TELEFONO_TEST, "user", mensaje)
            nota = (
                f"\n[Nota interna: en este mensaje se enviaron las fotos de: {', '.join(fotos_mandadas)}]"
                if fotos_mandadas
                else ""
            )
            await guardar_mensaje(TELEFONO_TEST, "assistant", respuesta + nota)


if __name__ == "__main__":
    asyncio.run(main())
