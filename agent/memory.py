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
from sqlalchemy import DateTime, Integer, String, Text, case, delete, func, select
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


async def registrar_mensaje_cliente(telefono: str, plataforma: str, texto: str):
    async with async_session() as session:
        fila = await session.get(Atencion, telefono)
        if not fila:
            fila = Atencion(telefono=telefono)
            session.add(fila)
        fila.plataforma = plataforma
        fila.ultimo_texto = texto[:500]
        fila.ultimo_en = ahora()
        await session.commit()


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


def config_ruta_defecto() -> dict:
    """La ruta habitual: una zona por hora desde la 1 PM; lunes, martes, jueves, sabado y domingo."""
    paradas = []
    for n, zona in enumerate(ORDEN_RUTA):
        paradas.append({"zona": zona, "inicio": f"{13 + n:02d}:00", "fin": f"{14 + n:02d}:00", "cupo": 3})
    return {"dias": [0, 1, 3, 5, 6], "paradas": paradas, "anticipacion_dias": 1, "semanas": 3, "bloqueados": []}


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
    return {**defecto, **{k: v for k, v in guardada.items() if k in defecto}}


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
                "ocupados": len(lista),
                "entregas": [{"id": e.id, "telefono": e.telefono, "nombre": e.nombre, "pedido": e.pedido, "estado": e.estado} for e in lista],
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
                    salida[f] = {"fecha": f, "horario": f"{parada['inicio']} a {parada['fin']}", "quedan": parada["cupo"] - usados}
    # Dias extra abiertos a mano (por ejemplo un dia fuera de la ruta)
    for r in await listar_rutas(desde_fecha):
        if r["fecha"] in salida:
            continue
        if any(_sin_acentos(x) == z for x in r["zonas"]) and r["cupo"] > r["ocupados"]:
            salida[r["fecha"]] = {"fecha": r["fecha"], "horario": r["horario"], "quedan": r["cupo"] - r["ocupados"]}
    return [salida[k] for k in sorted(salida)]


async def crear_entrega(telefono: str, fecha: str, zona: str, nombre: str, pedido: str, desde_fecha: str) -> tuple[bool, str]:
    """Agenda la entrega si ese dia hay ruta para la zona y queda lugar. Devuelve (ok, mensaje)."""
    disp = next((d for d in await dias_disponibles(zona, desde_fecha) if d["fecha"] == fecha), None)
    if not disp:
        return False, "Ese dia no hay entregas para esa zona o ya se lleno."
    async with async_session() as session:
        ya = (await session.execute(select(Entrega).where(
            Entrega.telefono == telefono, Entrega.fecha == fecha, Entrega.estado != "cancelada"
        ))).scalars().first()
        if ya:
            return True, disp["horario"]
        session.add(Entrega(telefono=telefono, fecha=fecha, zona=zona[:40], nombre=nombre[:100],
                            pedido=pedido[:600], estado="por_confirmar", creado_en=ahora()))
        await session.commit()
    return True, disp["horario"]


async def listar_entregas(desde_fecha: str) -> list[dict]:
    async with async_session() as session:
        filas = (await session.execute(
            select(Entrega).where(Entrega.fecha >= desde_fecha, Entrega.estado != "cancelada").order_by(Entrega.fecha, Entrega.id)
        )).scalars().all()
    return [{"id": f.id, "telefono": f.telefono, "fecha": f.fecha, "zona": f.zona, "nombre": f.nombre,
             "pedido": f.pedido, "estado": f.estado} for f in filas]


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


async def inicializar_db():
    """Crea las tablas si no existen."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


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
