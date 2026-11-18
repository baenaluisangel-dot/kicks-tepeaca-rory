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

        async def enviar_foto_simulada(producto_id: str, todas: bool = False) -> str:
            producto = obtener_producto(producto_id)
            if not producto:
                return f"ERROR: no existe el producto '{producto_id}'."
            rutas = producto["fotos"] if todas else producto["fotos"][:1]
            for n, ruta in enumerate(rutas, 1):
                print(f"\n  [FOTO] {pie_de_foto(producto, n, len(rutas))}  ({ruta})")
            fotos_mandadas.append(producto["nombre"] + (f" ({len(rutas)} fotos)" if len(rutas) > 1 else ""))
            return f"OK: {len(rutas)} foto(s) de {producto['nombre']} enviada(s)."

        async def enviar_catalogo_simulado() -> str:
            print("\n  [PDF] Catalogo_Juguetes_Kicks_Tepeaca.pdf")
            fotos_mandadas.append("el catálogo de juguetes en PDF")
            return "OK: catalogo en PDF enviado."

        print("\nAgente: ", end="", flush=True)
        respuesta, es_respuesta_real = await generar_respuesta(
            mensaje, historial, enviar_foto_simulada, enviar_catalogo_simulado
        )
        print(respuesta)
        print()

        # Igual que en produccion: los avisos tecnicos no entran al historial
        if es_respuesta_real:
            await guardar_mensaje(TELEFONO_TEST, "user", mensaje)
            nota = (
                f"\n[Nota interna: en este mensaje se enviaron estos archivos: {', '.join(fotos_mandadas)}]"
                if fotos_mandadas
                else ""
            )
            await guardar_mensaje(TELEFONO_TEST, "assistant", respuesta + nota)


if __name__ == "__main__":
    asyncio.run(main())
