# agent/memory.py — Memoria de conversaciones
# Generado por AgentKit

"""
Guarda el historial de cada conversacion por numero de telefono, y lleva registro de
que eventos de webhook ya se atendieron.

SQLite en local, PostgreSQL en produccion.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from sqlalchemy import Boolean, DateTime, Integer, String, Text, case, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

load_dotenv()
logger = logging.getLogger("agentkit")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./agentkit.db")

# Railway entrega la URL de PostgreSQL con el esquema "postgresql://" (o "postgres://").
# SQLAlchemy en modo asincrono necesita que el driver sea explicito.
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)

# En produccion, SQLite vive dentro del contenedor y el disco del contenedor es efimero:
# cada redespliegue borra el historial de todas las conversaciones. Avisarlo fuerte, porque
# el agente arranca igual y el problema recien se nota cuando un cliente vuelve a escribir.
if DATABASE_URL.startswith("sqlite") and os.getenv("ENVIRONMENT") == "production":
    logger.warning(
        "Estas en produccion con SQLite. El historial se va a borrar en cada redespliegue. "
        "Agrega PostgreSQL y configura DATABASE_URL para que el agente recuerde a sus clientes."
    )

engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def ahora() -> datetime:
    """Hora actual en UTC, con zona horaria."""
    return datetime.now(timezone.utc)


def como_utc(fecha: datetime | None) -> datetime | None:
    """
    Una fecha de la base, siempre con zona horaria.

    PostgreSQL devuelve las fechas con zona, pero SQLite las devuelve sin ella aunque
    la columna la declare. Restar una contra otra tira TypeError. Como todo lo que
    guardamos esta en UTC, a la que venga sin zona se le pone UTC.
    """
    if fecha is None:
        return None
    return fecha if fecha.tzinfo else fecha.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    pass


class Mensaje(Base):
    """Un mensaje del historial de conversacion."""

    __tablename__ = "mensajes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    role: Mapped[str] = mapped_column(String(20))  # "user" o "assistant"
    content: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)


class EventoProcesado(Base):
    """
    Eventos de webhook que ya se atendieron.

    Los proveedores entregan "al menos una vez": el mismo evento puede llegar dos veces.
    Sin esta tabla, el cliente recibiria la misma respuesta repetida.
    """

    __tablename__ = "eventos_procesados"

    evento_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora, index=True)


class ConversacionModo(Base):
    """Campana de la que viene cada conversacion ("puebla"). Sin fila = Tepeaca."""

    __tablename__ = "conversacion_modo"

    telefono: Mapped[str] = mapped_column(String(50), primary_key=True)
    modo: Mapped[str] = mapped_column(String(20))
    actualizado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)


async def obtener_modo(telefono: str) -> str:
    """Modo guardado de la conversacion; "tepeaca" si no hay ninguno."""
    async with async_session() as session:
        fila = await session.get(ConversacionModo, telefono)
    return fila.modo if fila else "tepeaca"


async def guardar_modo(telefono: str, modo: str):
    """Guarda (o actualiza) el modo de la conversacion."""
    async with async_session() as session:
        fila = await session.get(ConversacionModo, telefono)
        if fila:
            fila.modo = modo
            fila.actualizado_en = ahora()
        else:
            session.add(ConversacionModo(telefono=telefono, modo=modo, actualizado_en=ahora()))
        await session.commit()


class ConversacionPausa(Base):
    """Conversaciones donde Rory no contesta. hasta=None -> pausa indefinida."""

    __tablename__ = "conversacion_pausa"

    telefono: Mapped[str] = mapped_column(String(50), primary_key=True)
    hasta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


async def esta_pausada(telefono: str) -> bool:
    async with async_session() as session:
        fila = await session.get(ConversacionPausa, telefono)
    if not fila:
        return False
    if fila.hasta is None:
        return True
    hasta = fila.hasta if fila.hasta.tzinfo else fila.hasta.replace(tzinfo=timezone.utc)
    return hasta > ahora()


async def pausar(telefono: str, horas: float | None = None):
    """Pausa a Rory en la conversacion (horas=None: hasta que se reactive)."""
    hasta = ahora() + timedelta(hours=horas) if horas else None
    async with async_session() as session:
        fila = await session.get(ConversacionPausa, telefono)
        if fila:
            # una pausa indefinida no se acorta con una temporal
            if not (fila.hasta is None and hasta is not None):
                fila.hasta = hasta
        else:
            session.add(ConversacionPausa(telefono=telefono, hasta=hasta))
        await session.commit()


async def reanudar(telefono: str):
    async with async_session() as session:
        fila = await session.get(ConversacionPausa, telefono)
        if fila:
            await session.delete(fila)
            await session.commit()


class Atencion(Base):
    """Estado de cada conversacion para el panel del equipo."""

    __tablename__ = "atencion"

    telefono: Mapped[str] = mapped_column(String(50), primary_key=True)
    plataforma: Mapped[str] = mapped_column(String(20), default="whatsapp")
    ultimo_texto: Mapped[str] = mapped_column(Text, default="")
    ultimo_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)
    necesita_persona: Mapped[bool] = mapped_column(default=False)
    motivo: Mapped[str] = mapped_column(Text, default="")
    # Para responder, Zernio necesita conversation_id y account_id, no basta el telefono.
    # Rory los tenia solo dentro del webhook; guardandolos aqui, el despachador puede
    # escribirle a alguien despues, que es de lo que vive todo el seguimiento.
    contexto: Mapped[str] = mapped_column(Text, default="{}")


async def registrar_mensaje_cliente(
    telefono: str, plataforma: str, texto: str, contexto: dict | None = None
):
    import json
    async with async_session() as session:
        fila = await session.get(Atencion, telefono)
        if not fila:
            fila = Atencion(telefono=telefono)
            session.add(fila)
        fila.plataforma = plataforma
        fila.ultimo_texto = texto[:500]
        fila.ultimo_en = ahora()
        # Solo lo que hace falta para responder. El resto del contexto cambia en cada
        # evento y guardarlo entero seria ruido.
        if contexto:
            util = {k: v for k, v in contexto.items()
                    if k in ("conversation_id", "account_id", "plataforma") and v}
            if util:
                fila.contexto = json.dumps(util, ensure_ascii=False)
        await session.commit()


async def contexto_de(telefono: str) -> dict:
    """El contexto guardado para escribirle a ese cliente. Vacio si nunca escribio."""
    import json
    async with async_session() as session:
        fila = await session.get(Atencion, telefono)
    if not fila or not fila.contexto:
        return {}
    try:
        return json.loads(fila.contexto)
    except ValueError:
        return {}


async def horas_desde_su_ultimo_mensaje(telefono: str) -> float | None:
    """
    Cuantas horas pasaron desde que el cliente escribio. None si nunca lo hizo.

    Es el dato que decide si se le puede mandar texto libre: WhatsApp solo lo
    permite dentro de las 24 h del ultimo mensaje del cliente.
    """
    async with async_session() as session:
        fila = await session.get(Atencion, telefono)
    if not fila or not fila.ultimo_en:
        return None
    return (ahora() - como_utc(fila.ultimo_en)).total_seconds() / 3600


async def marcar_necesita_persona(telefono: str, necesita: bool, motivo: str = ""):
    async with async_session() as session:
        fila = await session.get(Atencion, telefono)
        if not fila:
            fila = Atencion(telefono=telefono)
            session.add(fila)
        fila.necesita_persona = necesita
        fila.motivo = motivo[:300] if necesita else ""
        await session.commit()


async def necesita_persona(telefono: str) -> bool:
    async with async_session() as session:
        fila = await session.get(Atencion, telefono)
    return bool(fila and fila.necesita_persona)


async def listar_atencion(limite: int = 200) -> list[dict]:
    """Conversaciones para el panel: primero las que necesitan persona, luego las recientes."""
    async with async_session() as session:
        filas = (await session.execute(select(Atencion))).scalars().all()
        pausas = {p.telefono: p for p in (await session.execute(select(ConversacionPausa))).scalars().all()}
    salida = []
    for f in filas:
        p = pausas.get(f.telefono)
        pausada = False
        if p:
            h = p.hasta
            pausada = h is None or (h if h.tzinfo else h.replace(tzinfo=timezone.utc)) > ahora()
        salida.append({
            "telefono": f.telefono, "plataforma": f.plataforma, "texto": f.ultimo_texto,
            "ultimo_en": f.ultimo_en, "necesita_persona": f.necesita_persona,
            "motivo": f.motivo, "pausada": pausada,
        })
    salida.sort(key=lambda x: x["ultimo_en"], reverse=True)
    salida.sort(key=lambda x: not x["necesita_persona"])
    return salida[:limite]


class Pendiente(Base):
    """Mensajes de clientes que Rory todavia no contesta (esperan su retraso).
    Se guardan para que un redespliegue o reinicio no los pierda."""

    __tablename__ = "pendientes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    texto: Mapped[str] = mapped_column(Text)
    mensaje_id: Mapped[str] = mapped_column(String(200), default="")
    contexto: Mapped[str] = mapped_column(Text, default="{}")
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)


async def guardar_pendiente(telefono: str, texto: str, mensaje_id: str, contexto: dict) -> int:
    import json
    async with async_session() as session:
        fila = Pendiente(
            telefono=telefono, texto=texto, mensaje_id=mensaje_id or "",
            contexto=json.dumps(contexto, ensure_ascii=False), creado_en=ahora(),
        )
        session.add(fila)
        await session.commit()
        return fila.id


async def borrar_pendientes(ids: list[int]):
    if not ids:
        return
    async with async_session() as session:
        await session.execute(delete(Pendiente).where(Pendiente.id.in_(ids)))
        await session.commit()


async def cargar_pendientes() -> list[dict]:
    import json
    async with async_session() as session:
        filas = (await session.execute(select(Pendiente).order_by(Pendiente.id))).scalars().all()
    salida = []
    for f in filas:
        try:
            ctx = json.loads(f.contexto or "{}")
        except ValueError:
            ctx = {}
        salida.append({
            "id": f.id, "telefono": f.telefono, "texto": f.texto,
            "mensaje_id": f.mensaje_id, "contexto": ctx, "creado_en": f.creado_en,
        })
    return salida


class Aviso(Base):
    """Cada vez que Rory pasa una conversacion al equipo (con tipo y motivo)."""

    __tablename__ = "avisos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    tipo: Mapped[str] = mapped_column(String(30), default="otro")
    motivo: Mapped[str] = mapped_column(Text, default="")
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora, index=True)


async def registrar_aviso(telefono: str, tipo: str, motivo: str):
    async with async_session() as session:
        session.add(Aviso(telefono=telefono, tipo=tipo or "otro", motivo=(motivo or "")[:500], creado_en=ahora()))
        await session.commit()


async def listar_avisos(desde: datetime) -> list[dict]:
    async with async_session() as session:
        filas = (await session.execute(
            select(Aviso).where(Aviso.creado_en >= desde).order_by(Aviso.id.desc())
        )).scalars().all()
    return [{"telefono": f.telefono, "tipo": f.tipo, "motivo": f.motivo, "creado_en": f.creado_en} for f in filas]


async def ultimo_aviso_por_telefono() -> dict[str, dict]:
    async with async_session() as session:
        filas = (await session.execute(select(Aviso).order_by(Aviso.id))).scalars().all()
    return {f.telefono: {"tipo": f.tipo, "motivo": f.motivo} for f in filas}  # el ultimo gana


async def transcripcion(telefono: str, limite: int = 200) -> list[dict]:
    """Toda la conversacion guardada (mas vieja primero), sin las notas internas."""
    import re
    async with async_session() as session:
        filas = (await session.execute(
            select(Mensaje).where(Mensaje.telefono == telefono).order_by(Mensaje.id.desc()).limit(limite)
        )).scalars().all()
    filas.reverse()
    return [
        {
            "role": f.role,
            "texto": re.sub(r"\n?\[Nota interna:.*?\]", "", f.content, flags=re.S).strip(),
            "hora": f.timestamp,
        }
        for f in filas
    ]


async def metricas(desde: datetime) -> dict:
    """Numeros del periodo: conversaciones nuevas, cuantas respondieron, avisos por tipo."""
    async with async_session() as session:
        filas = (await session.execute(
            select(
                Mensaje.telefono,
                func.min(Mensaje.timestamp),
                func.sum(case((Mensaje.role == "user", 1), else_=0)),
            ).group_by(Mensaje.telefono)
        )).all()
    def aware(d):
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    nuevas = [(t, int(n or 0)) for t, primero, n in filas if aware(primero) >= desde]
    def canal(t):
        return "instagram" if t.startswith("ig:") else "facebook" if t.startswith("fb:") else "whatsapp"
    por_canal: dict[str, dict] = {}
    for t, n in nuevas:
        c = por_canal.setdefault(canal(t), {"nuevas": 0, "respondieron": 0})
        c["nuevas"] += 1
        if n >= 2:
            c["respondieron"] += 1
    avisos = await listar_avisos(desde)
    por_tipo: dict[str, int] = {}
    for a in avisos:
        por_tipo[a["tipo"]] = por_tipo.get(a["tipo"], 0) + 1
    return {"por_canal": por_canal, "avisos": avisos, "avisos_por_tipo": por_tipo}


class Ruta(Base):
    """Un dia de entregas que el equipo abre: que zonas cubre, horario y cuantas caben."""

    __tablename__ = "rutas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fecha: Mapped[str] = mapped_column(String(10), index=True)   # YYYY-MM-DD
    zonas: Mapped[str] = mapped_column(Text, default="")          # separadas por coma
    horario: Mapped[str] = mapped_column(String(60), default="")
    cupo: Mapped[int] = mapped_column(Integer, default=5)


class Entrega(Base):
    """Una entrega agendada por Rory (por confirmar hasta que el equipo valide existencia)."""

    __tablename__ = "entregas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    fecha: Mapped[str] = mapped_column(String(10), index=True)
    zona: Mapped[str] = mapped_column(String(40), default="")
    nombre: Mapped[str] = mapped_column(String(100), default="")
    pedido: Mapped[str] = mapped_column(Text, default="")
    estado: Mapped[str] = mapped_column(String(20), default="por_confirmar")  # por_confirmar | confirmada | cancelada
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)
    # True = una sola pieza suelta que se lleva aprovechando una ruta que ya tiene un pedido grande
    suelto: Mapped[bool] = mapped_column(Boolean, default=False)


def _sin_acentos(t: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn").lower().strip()


async def crear_ruta(fecha: str, zonas: list[str], horario: str, cupo: int):
    async with async_session() as session:
        session.add(Ruta(fecha=fecha, zonas=",".join(zonas), horario=horario[:60], cupo=max(1, int(cupo))))
        await session.commit()


async def borrar_ruta(ruta_id: int):
    async with async_session() as session:
        await session.execute(delete(Ruta).where(Ruta.id == ruta_id))
        await session.commit()


async def _ocupados(session, fecha: str) -> int:
    n = await session.execute(
        select(func.count()).select_from(Entrega).where(Entrega.fecha == fecha, Entrega.estado != "cancelada")
    )
    return int(n.scalar() or 0)


async def listar_rutas(desde_fecha: str) -> list[dict]:
    async with async_session() as session:
        rutas = (await session.execute(
            select(Ruta).where(Ruta.fecha >= desde_fecha).order_by(Ruta.fecha, Ruta.id)
        )).scalars().all()
        salida = []
        for r in rutas:
            salida.append({
                "id": r.id, "fecha": r.fecha, "zonas": [z for z in r.zonas.split(",") if z],
                "horario": r.horario, "cupo": r.cupo, "ocupados": await _ocupados(session, r.fecha),
            })
    return salida


class Configuracion(Base):
    """Ajustes guardados como JSON (por ejemplo la ruta semanal de entregas)."""

    __tablename__ = "configuracion"

    clave: Mapped[str] = mapped_column(String(50), primary_key=True)
    valor: Mapped[str] = mapped_column(Text, default="{}")


ORDEN_RUTA = ["Amozoc", "Valsequillo", "CU", "Peri Plaza", "Angelópolis", "Centro", "CAPU"]

# Donde se entrega en cada zona (las piezas sueltas se recogen ahi). Vacio = el equipo lo confirma.
PUNTOS_DEFECTO = {
    "Amozoc": "Cerca de Chachapa: en la Bimbo o en el Little Caesars (donde está la gran bodega)",
    "Valsequillo": "AutoZone de Valsequillo",
    "CU": "Entrada principal de la universidad (CU)",
    "Peri Plaza": "Peri Plaza",
    "Angelópolis": "La Estrella de Puebla (La Rueda), en Angelópolis",
    "Centro": "Al lado de la catedral",
    "CAPU": "Dentro de la terminal de autobuses (CAPU)",
}


def config_ruta_defecto() -> dict:
    """La ruta habitual: una zona por hora desde la 1 PM; lunes, martes, jueves, sabado y domingo."""
    paradas = []
    for n, zona in enumerate(ORDEN_RUTA):
        paradas.append({"zona": zona, "inicio": f"{13 + n:02d}:00", "fin": f"{14 + n:02d}:00", "cupo": 3,
                        "punto": PUNTOS_DEFECTO.get(zona, "")})
    return {"dias": [0, 1, 3, 5, 6], "paradas": paradas, "anticipacion_dias": 1, "semanas": 3, "bloqueados": []}


async def guardar_contexto_dueno(contexto: dict):
    """Guarda el chat del dueño (conversation_id, account_id...) y cuando escribio por ultima vez."""
    import json
    datos = {"contexto": contexto, "ts": ahora().isoformat()}
    async with async_session() as session:
        fila = await session.get(Configuracion, "dueno_ctx")
        if fila:
            fila.valor = json.dumps(datos)
        else:
            session.add(Configuracion(clave="dueno_ctx", valor=json.dumps(datos)))
        await session.commit()


async def obtener_contexto_dueno(max_horas: float = 23.0) -> dict | None:
    """El chat del dueño, solo si escribio hace menos de ~24 h (si no, WhatsApp no deja escribirle)."""
    import json
    async with async_session() as session:
        fila = await session.get(Configuracion, "dueno_ctx")
    if not fila:
        return None
    try:
        datos = json.loads(fila.valor)
        ts = datetime.fromisoformat(datos["ts"])
    except (ValueError, KeyError):
        return None
    if ahora() - ts > timedelta(hours=max_horas):
        return None
    return datos.get("contexto")


async def obtener_config_ruta() -> dict:
    import json
    defecto = config_ruta_defecto()
    async with async_session() as session:
        fila = await session.get(Configuracion, "ruta_semanal")
    if not fila:
        return defecto
    try:
        guardada = json.loads(fila.valor)
    except ValueError:
        return defecto
    cfg = {**defecto, **{k: v for k, v in guardada.items() if k in defecto}}
    # Configuraciones guardadas antes de que existieran los puntos: se completan con los de fabrica
    for p in cfg["paradas"]:
        if not p.get("punto"):
            p["punto"] = PUNTOS_DEFECTO.get(p["zona"], "")
    return cfg


async def punto_de_entrega(zona: str) -> str:
    """Donde se entrega en esa zona ('' si todavia no esta definido)."""
    z = _sin_acentos(zona)
    cfg = await obtener_config_ruta()
    return next((p.get("punto", "") for p in cfg["paradas"] if _sin_acentos(p["zona"]) == z), "")


async def guardar_config_ruta(cfg: dict):
    import json
    async with async_session() as session:
        fila = await session.get(Configuracion, "ruta_semanal")
        if fila:
            fila.valor = json.dumps(cfg, ensure_ascii=False)
        else:
            session.add(Configuracion(clave="ruta_semanal", valor=json.dumps(cfg, ensure_ascii=False)))
        await session.commit()


def fechas_de_ruta(cfg: dict, hoy_iso: str) -> list[str]:
    """Fechas futuras en las que hay ruta (dias de la semana elegidos, sin bloqueados)."""
    from datetime import date, timedelta
    inicio = date.fromisoformat(hoy_iso) + timedelta(days=int(cfg.get("anticipacion_dias", 1)))
    salida = []
    for n in range(int(cfg.get("semanas", 3)) * 7):
        d = inicio + timedelta(days=n)
        if d.weekday() in cfg.get("dias", []) and d.isoformat() not in cfg.get("bloqueados", []):
            salida.append(d.isoformat())
    return salida


async def listar_agenda(hoy_iso: str) -> list[dict]:
    """Por cada dia de ruta: sus paradas (zona, horario, cupo) con las entregas agendadas."""
    cfg = await obtener_config_ruta()
    fechas = fechas_de_ruta(cfg, hoy_iso)
    if not fechas:
        return []
    async with async_session() as session:
        ents = (await session.execute(
            select(Entrega).where(Entrega.fecha.in_(fechas), Entrega.estado != "cancelada").order_by(Entrega.id)
        )).scalars().all()
    agenda = []
    for f in fechas:
        paradas = []
        for p in cfg["paradas"]:
            z = _sin_acentos(p["zona"])
            lista = [e for e in ents if e.fecha == f and _sin_acentos(e.zona) == z]
            paradas.append({
                "zona": p["zona"], "horario": f"{p['inicio']} a {p['fin']}", "cupo": p["cupo"],
                "punto": p.get("punto", ""), "ocupados": len(lista),
                "entregas": [{"id": e.id, "telefono": e.telefono, "nombre": e.nombre, "pedido": e.pedido, "estado": e.estado, "suelto": bool(e.suelto)} for e in lista],
            })
        agenda.append({"fecha": f, "paradas": paradas})
    return agenda


async def dias_disponibles(zona: str, desde_fecha: str) -> list[dict]:
    """Dias futuros con lugar para la zona: la ruta semanal mas los dias extra abiertos a mano."""
    z = _sin_acentos(zona)
    cfg = await obtener_config_ruta()
    salida: dict[str, dict] = {}
    parada = next((p for p in cfg["paradas"] if _sin_acentos(p["zona"]) == z), None)
    if parada:
        fechas = fechas_de_ruta(cfg, desde_fecha)
        if fechas:
            async with async_session() as session:
                ents = (await session.execute(
                    select(Entrega).where(Entrega.fecha.in_(fechas), Entrega.estado != "cancelada")
                )).scalars().all()
            for f in fechas:
                usados = sum(1 for e in ents if e.fecha == f and _sin_acentos(e.zona) == z)
                if parada["cupo"] > usados:
                    salida[f] = {"fecha": f, "horario": f"{parada['inicio']} a {parada['fin']}", "quedan": parada["cupo"] - usados, "punto": parada.get("punto", "")}
    # Dias extra abiertos a mano (por ejemplo un dia fuera de la ruta)
    for r in await listar_rutas(desde_fecha):
        if r["fecha"] in salida:
            continue
        if any(_sin_acentos(x) == z for x in r["zonas"]) and r["cupo"] > r["ocupados"]:
            salida[r["fecha"]] = {"fecha": r["fecha"], "horario": r["horario"], "quedan": r["cupo"] - r["ocupados"]}
    return [salida[k] for k in sorted(salida)]


async def rutas_con_pedido_grande(desde_fecha: str) -> list[dict]:
    """Dias y zonas de la ruta semanal que ya tienen un pedido grande (paquete o 2+ juguetes)
    agendado y todavia lugar: ahi se puede meter una pieza suelta porque ya se va a pasar."""
    cfg = await obtener_config_ruta()
    fechas = fechas_de_ruta(cfg, desde_fecha)
    if not fechas:
        return []
    async with async_session() as session:
        ents = (await session.execute(
            select(Entrega).where(Entrega.fecha.in_(fechas), Entrega.estado != "cancelada")
        )).scalars().all()
    salida = []
    for f in fechas:
        for p in cfg["paradas"]:
            z = _sin_acentos(p["zona"])
            de_la_parada = [e for e in ents if e.fecha == f and _sin_acentos(e.zona) == z]
            if any(not e.suelto for e in de_la_parada) and len(de_la_parada) < p["cupo"]:
                salida.append({"fecha": f, "zona": p["zona"], "horario": f"{p['inicio']} a {p['fin']}",
                               "quedan": p["cupo"] - len(de_la_parada), "punto": p.get("punto", "")})
    return salida


async def crear_entrega(telefono: str, fecha: str, zona: str, nombre: str, pedido: str, desde_fecha: str, suelto: bool = False) -> tuple[bool, str]:
    """Agenda la entrega si ese dia hay ruta para la zona y queda lugar. Una pieza suelta solo se
    agenda si esa parada ya tiene un pedido grande. Devuelve (ok, mensaje)."""
    disp = next((d for d in await dias_disponibles(zona, desde_fecha) if d["fecha"] == fecha), None)
    if not disp:
        return False, "Ese dia no hay entregas para esa zona o ya se lleno."
    if suelto:
        z = _sin_acentos(zona)
        if not any(r["fecha"] == fecha and _sin_acentos(r["zona"]) == z for r in await rutas_con_pedido_grande(desde_fecha)):
            return False, "Para una pieza suelta solo se puede en un dia y zona donde ya hay un pedido grande agendado."
    async with async_session() as session:
        ya = (await session.execute(select(Entrega).where(
            Entrega.telefono == telefono, Entrega.fecha == fecha, Entrega.estado != "cancelada"
        ))).scalars().first()
        if ya:
            return True, disp["horario"]
        session.add(Entrega(telefono=telefono, fecha=fecha, zona=zona[:40], nombre=nombre[:100],
                            pedido=pedido[:600], estado="por_confirmar", creado_en=ahora(), suelto=bool(suelto)))
        await session.commit()
    return True, disp["horario"]


async def listar_entregas(desde_fecha: str) -> list[dict]:
    async with async_session() as session:
        filas = (await session.execute(
            select(Entrega).where(Entrega.fecha >= desde_fecha, Entrega.estado != "cancelada").order_by(Entrega.fecha, Entrega.id)
        )).scalars().all()
    return [{"id": f.id, "telefono": f.telefono, "fecha": f.fecha, "zona": f.zona, "nombre": f.nombre,
             "pedido": f.pedido, "estado": f.estado, "suelto": bool(f.suelto)} for f in filas]


async def cambiar_estado_entrega(entrega_id: int, estado: str):
    async with async_session() as session:
        fila = await session.get(Entrega, entrega_id)
        if fila:
            fila.estado = estado
            await session.commit()


class Ejemplo(Base):
    """Como responde el dueño: se le muestran a Rory como ejemplos de estilo."""

    __tablename__ = "ejemplos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    modo: Mapped[str] = mapped_column(String(20), default="puebla")
    cliente: Mapped[str] = mapped_column(Text)
    respuesta: Mapped[str] = mapped_column(Text)
    nota: Mapped[str] = mapped_column(Text, default="")
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)


async def guardar_ejemplo(modo: str, cliente: str, respuesta: str, nota: str = ""):
    if not cliente.strip() or not respuesta.strip():
        return
    async with async_session() as session:
        session.add(Ejemplo(modo=modo if modo in ("puebla", "tepeaca") else "puebla",
                            cliente=cliente.strip()[:600], respuesta=respuesta.strip()[:900],
                            nota=(nota or "").strip()[:300], creado_en=ahora()))
        await session.commit()


async def listar_ejemplos(modo: str | None = None, limite: int = 200) -> list[dict]:
    async with async_session() as session:
        q = select(Ejemplo).order_by(Ejemplo.id.desc()).limit(limite)
        if modo:
            q = q.where(Ejemplo.modo == modo)
        filas = (await session.execute(q)).scalars().all()
    return [{"id": f.id, "modo": f.modo, "cliente": f.cliente, "respuesta": f.respuesta, "nota": f.nota} for f in filas]


async def borrar_ejemplo(ejemplo_id: int):
    async with async_session() as session:
        await session.execute(delete(Ejemplo).where(Ejemplo.id == ejemplo_id))
        await session.commit()


class Etiqueta(Base):
    """Lo que Claude entendio de una conversacion: que queria, que dudo, por que se enfrio."""

    __tablename__ = "etiquetas"

    telefono: Mapped[str] = mapped_column(String(50), primary_key=True)
    canal: Mapped[str] = mapped_column(String(20), default="whatsapp")
    productos: Mapped[str] = mapped_column(Text, default="[]")   # JSON: lista de nombres
    dudas: Mapped[str] = mapped_column(Text, default="[]")       # JSON: categorias fijas
    objecion: Mapped[str] = mapped_column(String(40), default="ninguna")
    resultado: Mapped[str] = mapped_column(String(30), default="en_curso")
    frases: Mapped[str] = mapped_column(Text, default="[]")      # JSON: frases textuales del cliente
    zona: Mapped[str] = mapped_column(String(60), default="")             # donde dijo estar
    en_zona: Mapped[str] = mapped_column(String(10), default="")          # si | no | no_dijo ("" = sin leer)
    producto_elegido: Mapped[str] = mapped_column(String(80), default="")  # lo que dijo querer
    ultimo_msg_id: Mapped[int] = mapped_column(Integer, default=0)
    actualizado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora, index=True)


class Informe(Base):
    """Un informe de 'Voz del cliente': numeros exactos + recomendaciones de Claude."""

    __tablename__ = "informes_voz"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora, index=True)
    dias: Mapped[int] = mapped_column(Integer, default=7)
    datos: Mapped[str] = mapped_column(Text, default="{}")       # JSON con metricas y sintesis


class Origen(Base):
    """El anuncio por el que escribio un cliente (una fila por cliente y anuncio)."""

    __tablename__ = "origenes_anuncio"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    canal: Mapped[str] = mapped_column(String(20), default="whatsapp")
    anuncio_id: Mapped[str] = mapped_column(String(100), default="", index=True)
    titulo: Mapped[str] = mapped_column(Text, default="")
    texto: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(Text, default="")
    datos: Mapped[str] = mapped_column(Text, default="{}")       # lo que mando Meta, completo
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora, index=True)


class Venta(Base):
    """Una venta cobrada, registrada por el equipo en el panel."""

    __tablename__ = "ventas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    monto: Mapped[int] = mapped_column(Integer)                   # pesos
    productos: Mapped[str] = mapped_column(Text, default="")
    entrega_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora, index=True)


async def agenda_reciente(telefono: str, minutos: int = 20) -> bool:
    """True si en los ultimos minutos Rory mando listas de entrega o agendo con este cliente."""
    async with async_session() as session:
        textos = (await session.execute(
            select(Mensaje.content).where(Mensaje.telefono == telefono, Mensaje.role == "assistant",
                                          Mensaje.timestamp >= ahora() - timedelta(minutes=minutos))
        )).scalars().all()
    return any(marca in t for t in textos for marca in ("lista de zonas", "lista de días", "lista de rutas", "agendada"))


async def guardar_origen(telefono: str, canal: str, anuncio: dict):
    """Guarda de que anuncio vino. El mismo cliente con el mismo anuncio no se repite."""
    anuncio_id, titulo = anuncio.get("anuncio_id", ""), anuncio.get("titulo", "")
    async with async_session() as session:
        previas = (await session.execute(select(Origen).where(Origen.telefono == telefono))).scalars().all()
        for p in previas:
            if (anuncio_id and p.anuncio_id == anuncio_id) or (not anuncio_id and p.datos == anuncio.get("datos")):
                # Llego en dos partes: se completa lo que faltaba
                for campo in ("titulo", "texto", "url"):
                    if not getattr(p, campo) and anuncio.get(campo):
                        setattr(p, campo, anuncio[campo])
                await session.commit()
                return
        session.add(Origen(telefono=telefono, canal=canal, anuncio_id=anuncio_id, titulo=titulo,
                           texto=anuncio.get("texto", ""), url=anuncio.get("url", ""),
                           datos=anuncio.get("datos", "{}"), creado_en=ahora()))
        await session.commit()


async def origenes_de(telefono: str) -> list[dict]:
    async with async_session() as session:
        filas = (await session.execute(
            select(Origen).where(Origen.telefono == telefono).order_by(Origen.id)
        )).scalars().all()
    return [{"anuncio_id": f.anuncio_id, "titulo": f.titulo, "texto": f.texto, "url": f.url,
             "creado_en": f.creado_en} for f in filas]


async def entregas_sin_venta(desde_fecha: str, hasta_fecha: str, telefono: str | None = None) -> list[dict]:
    """Entregas de ese rango (no canceladas) que todavia no tienen venta registrada."""
    async with async_session() as session:
        vendidas = select(Venta.entrega_id).where(Venta.entrega_id.is_not(None))
        q = select(Entrega).where(Entrega.fecha >= desde_fecha, Entrega.fecha <= hasta_fecha,
                                  Entrega.estado != "cancelada", Entrega.id.not_in(vendidas))
        if telefono:
            q = q.where(Entrega.telefono == telefono)
        filas = (await session.execute(q.order_by(Entrega.fecha, Entrega.id))).scalars().all()
    return [{"id": f.id, "telefono": f.telefono, "fecha": f.fecha, "zona": f.zona,
             "nombre": f.nombre, "pedido": f.pedido} for f in filas]


async def registrar_venta(telefono: str, monto: int, productos: str = "",
                          entrega_id: int | None = None) -> bool:
    """Registra una venta. False si ya estaba (misma entrega, o doble clic)."""
    async with async_session() as session:
        if entrega_id:
            q = select(Venta).where(Venta.entrega_id == entrega_id)
        else:
            q = select(Venta).where(Venta.telefono == telefono, Venta.monto == monto,
                                    Venta.creado_en >= ahora() - timedelta(minutes=2))
        if (await session.execute(q.limit(1))).scalar_one_or_none():
            return False
        session.add(Venta(telefono=telefono, monto=monto, productos=productos.strip()[:500],
                          entrega_id=entrega_id, creado_en=ahora()))
        await session.commit()
    return True


async def entrega_por_id(entrega_id: int) -> dict | None:
    async with async_session() as session:
        f = await session.get(Entrega, entrega_id)
    return {"id": f.id, "telefono": f.telefono, "pedido": f.pedido} if f else None


async def ventas_de(telefono: str) -> list[dict]:
    async with async_session() as session:
        filas = (await session.execute(
            select(Venta).where(Venta.telefono == telefono).order_by(Venta.id)
        )).scalars().all()
    return [{"id": f.id, "monto": f.monto, "productos": f.productos, "creado_en": f.creado_en} for f in filas]


async def borrar_venta(venta_id: int):
    async with async_session() as session:
        await session.execute(delete(Venta).where(Venta.id == venta_id))
        await session.commit()


async def total_ventas(desde: datetime) -> tuple[int, int]:
    """(cuantas ventas, cuanto dinero) desde esa fecha."""
    async with async_session() as session:
        n, total = (await session.execute(
            select(func.count(Venta.id), func.coalesce(func.sum(Venta.monto), 0))
            .where(Venta.creado_en >= desde.astimezone(timezone.utc))
        )).one()
    return int(n), int(total)


async def inicializar_db():
    """Crea las tablas si no existen y agrega las columnas nuevas a las tablas que ya existian."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # create_all no agrega columnas a una tabla vieja: se hace a mano, una por una
    existe = "IF NOT EXISTS " if engine.dialect.name == "postgresql" else ""
    sentencias = [
        f"ALTER TABLE {tabla} ADD COLUMN {existe}{columna}"
        for tabla, columna in (
            ("entregas", "suelto BOOLEAN DEFAULT FALSE"),
            ("etiquetas", "zona VARCHAR(60) DEFAULT ''"),
            ("etiquetas", "en_zona VARCHAR(10) DEFAULT ''"),
            ("etiquetas", "producto_elegido VARCHAR(80) DEFAULT ''"),
            ("atencion", "contexto TEXT DEFAULT '{}'"),
        )
    ]
    from sqlalchemy import text as _sql
    for sentencia in sentencias:
        try:
            async with engine.begin() as conn:
                await conn.execute(_sql(sentencia))
        except Exception:  # noqa: BLE001 — la columna ya existe
            pass


async def marcar_evento_procesado(evento_id: str) -> bool:
    """
    Registra un evento. Retorna True si es nuevo, False si ya se habia procesado.

    La unicidad la garantiza la base de datos (clave primaria), no una consulta previa:
    asi dos webhooks que llegan al mismo tiempo no pasan los dos.
    """
    if not evento_id:
        return True  # sin id no podemos deduplicar: se procesa

    async with async_session() as session:
        session.add(EventoProcesado(evento_id=evento_id, creado_en=ahora()))
        try:
            await session.commit()
            return True
        except IntegrityError:
            await session.rollback()
            return False


async def liberar_evento(evento_id: str):
    """
    Borra la marca de un evento para que el reintento del proveedor SI se procese.

    Se usa cuando el mensaje se marco como procesado pero despues fallo el envio de la
    respuesta. Sin esto, el reintento se descartaria por duplicado y el cliente se
    quedaria sin respuesta para siempre.
    """
    if not evento_id:
        return
    async with async_session() as session:
        await session.execute(delete(EventoProcesado).where(EventoProcesado.evento_id == evento_id))
        await session.commit()


async def limpiar_eventos_viejos(dias: int = 7):
    """Borra los eventos de hace mas de N dias para que la tabla no crezca sin fin."""
    limite = ahora() - timedelta(days=dias)
    async with async_session() as session:
        resultado = await session.execute(
            delete(EventoProcesado).where(EventoProcesado.creado_en < limite)
        )
        await session.commit()
    if resultado.rowcount:
        logger.info(f"Se limpiaron {resultado.rowcount} eventos de mas de {dias} dias")


async def guardar_mensaje(telefono: str, role: str, content: str):
    """Guarda un mensaje en el historial de esa conversacion."""
    async with async_session() as session:
        session.add(Mensaje(telefono=telefono, role=role, content=content, timestamp=ahora()))
        await session.commit()


async def obtener_historial(telefono: str, limite: int = 20) -> list[dict]:
    """
    Devuelve los ultimos N mensajes de una conversacion, en orden cronologico.

    Se ordena por id y no por timestamp: dos mensajes guardados en el mismo instante
    tienen el mismo timestamp, y el orden entre ellos quedaria librado al azar.
    """
    async with async_session() as session:
        resultado = await session.execute(
            select(Mensaje)
            .where(Mensaje.telefono == telefono)
            .order_by(Mensaje.id.desc())
            .limit(limite)
        )
        mensajes = list(resultado.scalars().all())

    mensajes.reverse()  # vienen del mas nuevo al mas viejo: los damos vuelta

    # La API de Claude exige que el historial empiece con un mensaje del usuario.
    # Si por un error anterior quedo un "assistant" suelto al principio, lo sacamos.
    while mensajes and mensajes[0].role != "user":
        mensajes.pop(0)

    return [{"role": m.role, "content": m.content} for m in mensajes]


async def limpiar_historial(telefono: str):
    """Borra todo el historial de una conversacion."""
    async with async_session() as session:
        await session.execute(delete(Mensaje).where(Mensaje.telefono == telefono))
        await session.commit()


# ── Cola de aprobaciones y bitacora ──────────────────────────────────────────
# Un aviso de Telegram es texto que se pierde en el scroll: si Luisito no contesta
# en ese momento, no queda en ningun lado que algo sigue detenido esperandolo.
# Estas dos tablas son la diferencia entre avisar y llevar la cuenta.


class Espera(Base):
    """Algo detenido esperando una decision de Luisito."""

    __tablename__ = "esperando"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agente: Mapped[str] = mapped_column(String(30), index=True)
    asunto: Mapped[str] = mapped_column(Text)                 # que se le pide aprobar
    detalle: Mapped[str] = mapped_column(Text, default="")    # contexto para decidir
    opciones: Mapped[str] = mapped_column(Text, default="[]")  # JSON: [{clave, etiqueta}]
    # Pesos que se dejan de ganar si esto no se contesta. Ordenar por aqui y no por
    # fecha es lo que hace util la cola: lo viejo no siempre es lo que mas cuesta.
    costo_esperar: Mapped[int] = mapped_column(Integer, default=0)
    telefono: Mapped[str] = mapped_column(String(50), default="", index=True)
    estado: Mapped[str] = mapped_column(String(20), default="pendiente", index=True)
    respuesta: Mapped[str] = mapped_column(String(40), default="")
    mensaje_telegram: Mapped[int] = mapped_column(Integer, default=0)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora, index=True)
    resuelto_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Bitacora(Base):
    """Que hizo cada agente en cada ejecucion. Un agente que no hizo nada tambien
    escribe aqui: el silencio es indistinguible de una falla."""

    __tablename__ = "bitacora"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agente: Mapped[str] = mapped_column(String(30), index=True)
    accion: Mapped[str] = mapped_column(Text)
    motivo: Mapped[str] = mapped_column(Text, default="")
    resultado: Mapped[str] = mapped_column(String(20), default="ok")  # ok | escalado | error
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora, index=True)


async def crear_espera(
    agente: str,
    asunto: str,
    detalle: str = "",
    opciones: list[dict] | None = None,
    costo_esperar: int = 0,
    telefono: str = "",
) -> int:
    """Encola una decision y devuelve su id. Las opciones son [{clave, etiqueta}]."""
    import json
    async with async_session() as session:
        fila = Espera(
            agente=agente, asunto=asunto, detalle=detalle,
            opciones=json.dumps(opciones or [], ensure_ascii=False),
            costo_esperar=int(costo_esperar), telefono=telefono or "",
            estado="pendiente", creado_en=ahora(),
        )
        session.add(fila)
        await session.commit()
        return fila.id


async def listar_esperas(estado: str = "pendiente", limite: int = 50) -> list[dict]:
    """Lo detenido, lo que mas cuesta primero."""
    import json
    async with async_session() as session:
        consulta = select(Espera).order_by(Espera.costo_esperar.desc(), Espera.creado_en).limit(limite)
        if estado:
            consulta = consulta.where(Espera.estado == estado)
        filas = (await session.execute(consulta)).scalars().all()
    salida = []
    for f in filas:
        try:
            opciones = json.loads(f.opciones or "[]")
        except ValueError:
            opciones = []
        salida.append({
            "id": f.id, "agente": f.agente, "asunto": f.asunto, "detalle": f.detalle,
            "opciones": opciones, "costo_esperar": f.costo_esperar, "telefono": f.telefono,
            "estado": f.estado, "respuesta": f.respuesta, "creado_en": f.creado_en,
            "mensaje_telegram": f.mensaje_telegram,
        })
    return salida


async def obtener_espera(espera_id: int) -> dict | None:
    filas = [e for e in await listar_esperas(estado="", limite=1000) if e["id"] == espera_id]
    return filas[0] if filas else None


async def resolver_espera(espera_id: int, respuesta: str) -> dict | None:
    """
    Marca una espera como resuelta. Devuelve la fila, o None si no existe o si alguien
    ya la contesto: asi dos toques seguidos del mismo boton no la resuelven dos veces.
    """
    async with async_session() as session:
        fila = await session.get(Espera, espera_id)
        if fila is None or fila.estado != "pendiente":
            return None
        fila.estado = "resuelta"
        fila.respuesta = respuesta[:40]
        fila.resuelto_en = ahora()
        await session.commit()
        return {"id": fila.id, "agente": fila.agente, "asunto": fila.asunto,
                "respuesta": fila.respuesta, "mensaje_telegram": fila.mensaje_telegram,
                "costo_esperar": fila.costo_esperar}


async def guardar_mensaje_telegram(espera_id: int, mensaje_telegram: int):
    """Guarda el id del mensaje de Telegram para poder editarlo cuando se conteste."""
    async with async_session() as session:
        fila = await session.get(Espera, espera_id)
        if fila is not None:
            fila.mensaje_telegram = int(mensaje_telegram)
            await session.commit()


async def registrar_bitacora(agente: str, accion: str, motivo: str = "", resultado: str = "ok"):
    """Deja constancia de una ejecucion. Nunca revienta al que la llama."""
    try:
        async with async_session() as session:
            session.add(Bitacora(agente=agente, accion=accion, motivo=motivo,
                                 resultado=resultado, creado_en=ahora()))
            await session.commit()
    except Exception as e:  # noqa: BLE001 — la bitacora no debe tumbar al agente
        logger.error(f"No se pudo escribir en la bitacora: {e}")


async def listar_bitacora(limite: int = 50, agente: str = "") -> list[dict]:
    async with async_session() as session:
        consulta = select(Bitacora).order_by(Bitacora.id.desc()).limit(limite)
        if agente:
            consulta = consulta.where(Bitacora.agente == agente)
        filas = (await session.execute(consulta)).scalars().all()
    return [{"id": f.id, "agente": f.agente, "accion": f.accion, "motivo": f.motivo,
             "resultado": f.resultado, "creado_en": f.creado_en} for f in filas]


# ── Cola de envio ────────────────────────────────────────────────────────────
# El agente de Cierre decide a quien escribirle y lo deja aqui; el despachador
# lo manda. Estan separados a proposito: el agente corre por lotes en la nube y
# el despachador vive en Railway, porque mandar depende de la ventana de 24 h y
# eso hay que mirarlo en el momento del envio, no cuando se decidio.


class Envio(Base):
    """Un mensaje listo para salir."""

    __tablename__ = "cola_envio"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    agente: Mapped[str] = mapped_column(String(30), default="cierre")
    texto: Mapped[str] = mapped_column(Text, default="")
    # Adjunto opcional, de las bibliotecas que ya viven en static/
    adjunto_tipo: Mapped[str] = mapped_column(String(10), default="")   # video | audio | foto
    adjunto_url: Mapped[str] = mapped_column(Text, default="")
    motivo: Mapped[str] = mapped_column(Text, default="")               # por que se manda
    programado_para: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora, index=True)
    estado: Mapped[str] = mapped_column(String(20), default="pendiente", index=True)
    # pendiente | enviado | requiere_plantilla | fallido | cancelado
    intentos: Mapped[int] = mapped_column(Integer, default=0)
    detalle: Mapped[str] = mapped_column(Text, default="")              # por que fallo
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)
    enviado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


MAX_INTENTOS = 3


async def encolar_envio(
    telefono: str,
    texto: str,
    agente: str = "cierre",
    motivo: str = "",
    adjunto_tipo: str = "",
    adjunto_url: str = "",
    demora_horas: float = 0,
) -> int:
    """Deja un mensaje listo para salir. demora_horas lo programa mas adelante."""
    async with async_session() as session:
        fila = Envio(
            telefono=telefono, agente=agente, texto=texto, motivo=motivo,
            adjunto_tipo=adjunto_tipo, adjunto_url=adjunto_url,
            programado_para=ahora() + timedelta(hours=demora_horas),
            estado="pendiente", creado_en=ahora(),
        )
        session.add(fila)
        await session.commit()
        return fila.id


async def envios_por_salir(limite: int = 20) -> list[dict]:
    """Lo que ya toca mandar: pendiente, con su hora cumplida y con intentos de sobra."""
    async with async_session() as session:
        filas = (await session.execute(
            select(Envio)
            .where(Envio.estado == "pendiente")
            .where(Envio.programado_para <= ahora())
            .where(Envio.intentos < MAX_INTENTOS)
            .order_by(Envio.programado_para)
            .limit(limite)
        )).scalars().all()
    return [{"id": f.id, "telefono": f.telefono, "agente": f.agente, "texto": f.texto,
             "adjunto_tipo": f.adjunto_tipo, "adjunto_url": f.adjunto_url,
             "motivo": f.motivo, "intentos": f.intentos} for f in filas]


async def tomar_envio(envio_id: int) -> bool:
    """
    Aparta un envio antes de mandarlo, subiendole el contador de intentos.

    Pasa de "pendiente" a "enviando" en una sola transaccion: si dos despachadores
    corren a la vez, solo uno se lo lleva y el cliente no recibe el mensaje dos veces.
    Devuelve False si otro ya lo tomo.
    """
    async with async_session() as session:
        fila = await session.get(Envio, envio_id)
        if fila is None or fila.estado != "pendiente":
            return False
        fila.estado = "enviando"
        fila.intentos += 1
        await session.commit()
        return True


async def marcar_envio(envio_id: int, estado: str, detalle: str = ""):
    """
    Cierra un envio. Un fallo con intentos de sobra vuelve a "pendiente" para
    reintentarse; sin intentos de sobra se queda en "fallido" y alguien lo mira.
    """
    async with async_session() as session:
        fila = await session.get(Envio, envio_id)
        if fila is None:
            return
        if estado == "fallido" and fila.intentos < MAX_INTENTOS:
            fila.estado = "pendiente"
            # Se espera un poco mas en cada reintento, por si la falla es pasajera
            fila.programado_para = ahora() + timedelta(minutes=15 * fila.intentos)
        else:
            fila.estado = estado
        fila.detalle = (detalle or "")[:500]
        if estado == "enviado":
            fila.enviado_en = ahora()
        await session.commit()


async def cancelar_envios_de(telefono: str) -> int:
    """
    Cancela lo que quedaba por mandarle a alguien. Se usa cuando el cliente contesta:
    el seguimiento automatico deja de tener sentido y Rory retoma la conversacion.
    """
    async with async_session() as session:
        resultado = await session.execute(
            select(Envio).where(Envio.telefono == telefono).where(Envio.estado == "pendiente")
        )
        filas = resultado.scalars().all()
        for f in filas:
            f.estado = "cancelado"
            f.detalle = "el cliente contesto"
        await session.commit()
        return len(filas)


async def resumen_cola() -> dict:
    """Cuantos envios hay en cada estado. Para el panel y el resumen diario."""
    async with async_session() as session:
        filas = (await session.execute(
            select(Envio.estado, func.count(Envio.id)).group_by(Envio.estado)
        )).all()
    return {estado: total for estado, total in filas}


# ── Observaciones de mercado ─────────────────────────────────────────────────
# Lo que el scraper ve afuera: precios, productos y senales de TikTok y otras
# fuentes. Se guarda CRUDO, una fila por video o post, no resumido: un resumen
# pierde la posibilidad de volver a analizar cuando cambia la pregunta, y nadie
# puede verificar de donde salio un numero.
#
# El scraper observa; el Agente de Mercado concluye. No al reves.


class Observacion(Base):
    """Un video, live o post que el scraper vio."""

    __tablename__ = "observaciones_mercado"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fuente: Mapped[str] = mapped_column(String(20), index=True)   # tiktok | tiktok_live | otro
    # La url es la huella: si el scraper pasa dos veces por el mismo video, no se duplica.
    url: Mapped[str] = mapped_column(String(500), unique=True)
    autor: Mapped[str] = mapped_column(String(120), default="")
    publicado_en: Mapped[str] = mapped_column(String(10), default="")   # YYYY-MM-DD
    texto: Mapped[str] = mapped_column(Text, default="")                # caption + transcripcion
    producto: Mapped[str] = mapped_column(String(160), default="", index=True)
    precio: Mapped[int] = mapped_column(Integer, default=0)
    moneda: Mapped[str] = mapped_column(String(5), default="MXN")
    # Confundir mayoreo con menudeo es la forma mas rapida de poner mal los precios.
    # Si el scraper no puede distinguirlo, pone no_claro y no adivina.
    tipo_precio: Mapped[str] = mapped_column(String(12), default="no_claro")
    ubicacion: Mapped[str] = mapped_column(String(80), default="")
    vistas: Mapped[int] = mapped_column(Integer, default=0)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    comentarios: Mapped[int] = mapped_column(Integer, default=0)
    # evaluar = precios de lo que ya vendemos; explorar = productos que todavia no
    proposito: Mapped[str] = mapped_column(String(10), default="evaluar", index=True)
    crudo: Mapped[str] = mapped_column(Text, default="{}")
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora, index=True)


FUENTES = ("tiktok", "tiktok_live", "otro")
TIPOS_PRECIO = ("menudeo", "mayoreo", "no_claro")
PROPOSITOS = ("evaluar", "explorar")


def _entero(valor) -> int:
    try:
        return max(0, int(float(valor)))
    except (TypeError, ValueError):
        return 0


async def guardar_observaciones(filas: list[dict]) -> dict:
    """
    Guarda un lote del scraper. Devuelve cuantas entraron, cuantas ya estaban y
    cuantas se rechazaron, con el motivo de las primeras rechazadas.

    Valida en vez de confiar: lo que llega lo produjo otra maquina leyendo texto
    de desconocidos, asi que puede venir incompleto o mal formado.
    """
    import json
    guardadas = repetidas = 0
    rechazos: list[str] = []

    for i, fila in enumerate(filas):
        url = str(fila.get("url") or "").strip()[:500]
        if not url.startswith(("http://", "https://")):
            if len(rechazos) < 5:
                rechazos.append(f"fila {i}: sin url valida")
            continue

        fuente = str(fila.get("fuente") or "otro").strip().lower()
        if fuente not in FUENTES:
            fuente = "otro"
        tipo = str(fila.get("tipo_precio") or "no_claro").strip().lower()
        if tipo not in TIPOS_PRECIO:
            tipo = "no_claro"
        proposito = str(fila.get("proposito") or "evaluar").strip().lower()
        if proposito not in PROPOSITOS:
            proposito = "evaluar"

        try:
            async with async_session() as session:
                session.add(Observacion(
                    fuente=fuente, url=url,
                    autor=str(fila.get("autor") or "")[:120],
                    publicado_en=str(fila.get("publicado_en") or "")[:10],
                    # El texto se guarda tal cual, pero acotado: una transcripcion
                    # larguisima no aporta mas y si infla la base.
                    texto=str(fila.get("texto") or "")[:8000],
                    producto=str(fila.get("producto") or "")[:160],
                    precio=_entero(fila.get("precio")),
                    moneda=str(fila.get("moneda") or "MXN")[:5],
                    tipo_precio=tipo,
                    ubicacion=str(fila.get("ubicacion") or "")[:80],
                    vistas=_entero(fila.get("vistas")),
                    likes=_entero(fila.get("likes")),
                    comentarios=_entero(fila.get("comentarios")),
                    proposito=proposito,
                    crudo=json.dumps(fila.get("crudo") or {}, ensure_ascii=False)[:4000],
                    creado_en=ahora(),
                ))
                await session.commit()
                guardadas += 1
        except IntegrityError:
            repetidas += 1   # ya habiamos visto ese video

    return {"guardadas": guardadas, "repetidas": repetidas,
            "rechazadas": len(filas) - guardadas - repetidas, "motivos": rechazos}


async def observaciones(
    proposito: str = "", producto: str = "", desde_dias: int = 30, limite: int = 200
) -> list[dict]:
    """Lo que el scraper vio, lo mas reciente primero."""
    corte = ahora() - timedelta(days=desde_dias)
    async with async_session() as session:
        consulta = select(Observacion).where(Observacion.creado_en >= corte)
        if proposito:
            consulta = consulta.where(Observacion.proposito == proposito)
        if producto:
            consulta = consulta.where(Observacion.producto.ilike(f"%{producto}%"))
        filas = (await session.execute(
            consulta.order_by(Observacion.creado_en.desc()).limit(limite)
        )).scalars().all()
    return [{"id": f.id, "fuente": f.fuente, "url": f.url, "autor": f.autor,
             "publicado_en": f.publicado_en, "texto": f.texto, "producto": f.producto,
             "precio": f.precio, "moneda": f.moneda, "tipo_precio": f.tipo_precio,
             "ubicacion": f.ubicacion, "vistas": f.vistas, "likes": f.likes,
             "comentarios": f.comentarios, "proposito": f.proposito,
             "creado_en": f.creado_en} for f in filas]


async def precios_observados(producto: str, tipo_precio: str = "menudeo") -> dict:
    """
    Que precios se han visto afuera para un producto. Devuelve el conteo y el rango,
    no un promedio solo: con tres observaciones un promedio enga\u00f1a mas de lo que informa.
    """
    filas = [o for o in await observaciones(producto=producto, limite=500)
             if o["tipo_precio"] == tipo_precio and o["precio"] > 0]
    if not filas:
        return {"n": 0}
    precios = sorted(o["precio"] for o in filas)
    medio = precios[len(precios) // 2]
    return {"n": len(precios), "min": precios[0], "max": precios[-1], "mediana": medio,
            "fuentes": [o["url"] for o in filas[:5]]}
