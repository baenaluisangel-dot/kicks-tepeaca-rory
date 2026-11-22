# agent/panel.py — Panel web del equipo (resumen, conversaciones, entregas, chat y práctica)

"""
Todo lo que ve el equipo: un panel con navegación, diseño responsive y modo oscuro.
Se protege con la variable PANEL_TOKEN (enlace privado). Sin ella, el panel no existe.
"""

import asyncio
import hmac
import html
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from agent.brain import generar_respuesta
from agent.catalogo import ZONAS_PUEBLA, obtener_producto
from agent.memory import (
    borrar_ejemplo,
    borrar_ruta,
    cambiar_estado_entrega,
    crear_ruta,
    dias_disponibles,
    guardar_ejemplo,
    listar_atencion,
    listar_ejemplos,
    listar_entregas,
    listar_rutas,
    marcar_necesita_persona,
    metricas,
    pausar,
    reanudar,
    transcripcion,
    ultimo_aviso_por_telefono,
)
from agent.notificar import avisar_por_telegram, enlace_cliente

logger = logging.getLogger("agentkit")
router = APIRouter()

def panel_autorizado(token: str) -> bool:
    esperado = os.getenv("PANEL_TOKEN", "")
    return bool(esperado) and hmac.compare_digest(token or "", esperado)


def _exigir(token: str):
    if not panel_autorizado(token):
        raise HTTPException(status_code=404)


# ── Fechas (hora de Mexico: UTC-6 fijo, sin horario de verano desde 2022) ────────

def zona_mx():
    return timezone(timedelta(hours=-6))


def inicio_de_hoy() -> datetime:
    return datetime.now(zona_mx()).replace(hour=0, minute=0, second=0, microsecond=0)


DIAS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES_ES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
            "septiembre", "octubre", "noviembre", "diciembre"]


def fecha_hoy_iso() -> str:
    return inicio_de_hoy().strftime("%Y-%m-%d")


def fecha_bonita(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{DIAS_ES[d.weekday()]} {d.day} de {MESES_ES[d.month - 1]}"


def _aware(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def esperando_txt(desde: datetime) -> str:
    mins = int((datetime.now(timezone.utc) - _aware(desde)).total_seconds() // 60)
    if mins < 60:
        return f"hace {mins} min"
    if mins < 1440:
        return f"hace {mins // 60} h"
    return f"hace {mins // 1440} d"


NOMBRES_TIPO = {
    "pedido": "🟢 Pedido listo", "duda_producto": "🟠 Duda de producto",
    "fuera_de_catalogo": "🟡 Pide algo que no tenemos", "queja_o_problema": "🔴 Queja o problema",
    "otro": "🔵 Otro",
}
CLASE_TIPO = {"pedido": "ok", "duda_producto": "warn", "fuera_de_catalogo": "warn",
              "queja_o_problema": "bad", "otro": "info"}


# ── Resumen para Telegram ────────────────────────────────────────────────────

async def resumen_texto() -> str:
    desde = inicio_de_hoy()
    m = await metricas(desde)
    lineas = [f"📊 Resumen de hoy ({desde.strftime('%d/%m')})"]
    for canal, c in m["por_canal"].items():
        pct = round(100 * c["respondieron"] / c["nuevas"]) if c["nuevas"] else 0
        lineas.append(f"{canal.capitalize()}: {c['nuevas']} conversaciones nuevas, {c['respondieron']} respondieron ({pct}%)")
    if not m["por_canal"]:
        lineas.append("Sin conversaciones nuevas.")
    if m["avisos_por_tipo"]:
        lineas.append("Pasadas al equipo: " + ", ".join(f"{NOMBRES_TIPO.get(t, t)} {n}" for t, n in m["avisos_por_tipo"].items()))
    esperando = [c for c in await listar_atencion() if c["necesita_persona"]]
    lineas.append(f"Esperando a una persona ahora: {len(esperando)}")
    for c in esperando[:10]:
        lineas.append(f"• {enlace_cliente(c['telefono'], c['plataforma'])} — {c['motivo'][:90]}")
    manana = (inicio_de_hoy() + timedelta(days=1)).strftime("%Y-%m-%d")
    ent = [e for e in await listar_entregas(manana) if e["fecha"] == manana]
    if ent:
        lineas.append(f"🚚 Entregas de mañana ({fecha_bonita(manana)}): {len(ent)}")
        for e in ent[:15]:
            lineas.append(f"• {e['nombre']} — {e['zona']} — {e['pedido'][:70]} ({'confirmada' if e['estado'] == 'confirmada' else 'por confirmar'})")
    return "\n".join(lineas)


async def bucle_resumen_diario():
    """Manda el resumen por Telegram todos los dias a HORA_RESUMEN (hora de Mexico)."""
    hora = int(os.getenv("HORA_RESUMEN") or "21")
    while True:
        ahora_mx = datetime.now(zona_mx())
        objetivo = ahora_mx.replace(hour=hora, minute=0, second=0, microsecond=0)
        if objetivo <= ahora_mx:
            objetivo += timedelta(days=1)
        await asyncio.sleep((objetivo - ahora_mx).total_seconds())
        try:
            await avisar_por_telegram(await resumen_texto())
        except Exception as e:  # noqa: BLE001
            logger.error(f"No se pudo mandar el resumen diario: {e}")


# ── Diseño ───────────────────────────────────────────────────────────────────

CSS = """
:root{--bg:#f4f3ef;--card:#fff;--ink:#1c1b19;--mut:#6b6a66;--line:#e6e3dc;--acc:#0f766e;--acc2:#0d5f58;--accink:#fff;
--ok:#166534;--okbg:#e6f4ea;--warn:#92400e;--warnbg:#fdf0dc;--bad:#b91c1c;--badbg:#fdecec;--info:#1e40af;--infobg:#e8eefc;--chip:#efede7;--shadow:0 1px 2px rgba(0,0,0,.05),0 4px 14px rgba(0,0,0,.04)}
@media(prefers-color-scheme:dark){:root{--bg:#141413;--card:#1f1e1c;--ink:#f1efe9;--mut:#a3a19a;--line:#34322e;--acc:#2dd4bf;--acc2:#5eead4;--accink:#06201c;
--ok:#86efac;--okbg:#16301f;--warn:#fcd34d;--warnbg:#38290f;--bad:#fca5a5;--badbg:#3a1f1f;--info:#93c5fd;--infobg:#1b2740;--chip:#2a2926;--shadow:none}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
header{position:sticky;top:0;z-index:5;background:var(--card);border-bottom:1px solid var(--line)}
.bar{max-width:1120px;margin:0 auto;padding:10px 16px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.brand{font-weight:700;font-size:17px;display:flex;align-items:center;gap:8px}.brand span{background:var(--acc);color:var(--accink);border-radius:8px;padding:2px 8px;font-size:14px}
nav{display:flex;gap:4px;overflow-x:auto;flex:1}nav a{padding:7px 12px;border-radius:999px;color:var(--mut);font-weight:500;white-space:nowrap}
nav a:hover{background:var(--chip);text-decoration:none}nav a.on{background:var(--acc);color:var(--accink)}
main{max-width:1120px;margin:0 auto;padding:20px 16px 60px}h1{font-size:24px;margin:4px 0 4px}h2{font-size:17px;margin:28px 0 10px}
.sub{color:var(--mut);margin:0 0 18px}.grid{display:grid;gap:12px}.stats{grid-template-columns:repeat(auto-fit,minmax(170px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px;box-shadow:var(--shadow)}
.stat .n{font-size:30px;font-weight:700;line-height:1.1}.stat .t{color:var(--mut);font-size:13px;margin-top:2px}.stat.alert{border-color:var(--bad);background:var(--badbg)}
.badge{display:inline-block;border-radius:999px;padding:2px 10px;font-size:12px;font-weight:600}
.badge.ok{background:var(--okbg);color:var(--ok)}.badge.warn{background:var(--warnbg);color:var(--warn)}.badge.bad{background:var(--badbg);color:var(--bad)}.badge.info{background:var(--infobg);color:var(--info)}.badge.mut{background:var(--chip);color:var(--mut)}
.btn,button{font:inherit;cursor:pointer;border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--ink);padding:6px 12px;font-weight:500}
.btn:hover,button:hover{border-color:var(--acc)}.btn.pri,button.pri{background:var(--acc);border-color:var(--acc);color:var(--accink)}.btn.pri:hover,button.pri:hover{background:var(--acc2)}
button.bad{color:var(--bad)}form.inl{display:inline}.acts{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.quote{background:var(--chip);border-radius:10px;padding:8px 12px;margin-top:8px;color:var(--ink)}.mut{color:var(--mut)}.small{font-size:13px}
.chips{display:flex;gap:6px;flex-wrap:wrap}.chip{background:var(--chip);border-radius:999px;padding:3px 11px;font-size:13px;display:inline-block}
.chip.sel{background:var(--acc);color:var(--accink)}label.chip{cursor:pointer}label.chip input{margin-right:4px}
.toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
input[type=text],input[type=search],input[type=date],input[type=number],textarea{font:inherit;border:1px solid var(--line);border-radius:10px;padding:8px 10px;background:var(--card);color:var(--ink);max-width:100%}
textarea{width:100%}.search{flex:1;min-width:200px}
.conv{display:grid;grid-template-columns:44px 1fr auto;gap:12px;align-items:center;padding:12px 14px;border-bottom:1px solid var(--line)}
.conv:last-child{border-bottom:0}.av{width:44px;height:44px;border-radius:50%;background:var(--chip);display:grid;place-items:center;font-size:20px}
.conv .who{font-weight:600}.conv .last{color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:62vw}
.list{background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden;box-shadow:var(--shadow)}
.bar-cupo{height:8px;background:var(--chip);border-radius:999px;overflow:hidden;margin:6px 0}.bar-cupo i{display:block;height:100%;background:var(--acc)}
.dia{display:grid;gap:8px}.dia h3{margin:0;font-size:17px}.ent{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:8px 0;border-top:1px solid var(--line)}
.toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);background:var(--ink);color:var(--bg);padding:10px 18px;border-radius:999px;box-shadow:0 6px 22px rgba(0,0,0,.25);z-index:20;transition:opacity .4s}
.chat{display:flex;flex-direction:column;gap:6px;max-width:760px;margin:0 auto}.b{padding:8px 12px;border-radius:14px;max-width:86%;white-space:pre-wrap;word-wrap:break-word}
.b small{display:block;color:var(--mut);font-size:11px;margin-bottom:2px}.b.cli{background:var(--card);border:1px solid var(--line);align-self:flex-start;border-bottom-left-radius:4px}
.b.rory{background:var(--okbg);align-self:flex-end;border-bottom-right-radius:4px}.acc{align-self:flex-end;font-size:13px;color:var(--mut);max-width:86%}
details summary{cursor:pointer;color:var(--acc);font-size:13px;margin-top:6px}details form{margin-top:6px;display:grid;gap:6px}
.empty{padding:26px;text-align:center;color:var(--mut)}@media(max-width:640px){h1{font-size:21px}.stat .n{font-size:26px}.conv{grid-template-columns:40px 1fr}.conv .acts{grid-column:1/-1}}
"""

NAV = [("resumen", "Resumen", "/panel"), ("conversaciones", "Conversaciones", "/panel/conversaciones"),
       ("entregas", "Entregas", "/panel/entregas"), ("practica", "Practicar con Rory", "/panel/practica")]


def pagina(token: str, activo: str, titulo: str, cuerpo: str, ok: str = "", extra_head: str = "", subtitulo: str = "") -> HTMLResponse:
    nav = "".join(
        f'<a class="{"on" if k == activo else ""}" href="{ruta}?token={quote(token)}">{nombre}</a>' for k, nombre, ruta in NAV
    )
    toast = f'<div class="toast" id="toast">{html.escape(ok)}</div><script>setTimeout(()=>{{const t=document.getElementById("toast");if(t){{t.style.opacity=0;setTimeout(()=>t.remove(),500)}}}},2600)</script>' if ok else ""
    sub = f'<p class="sub">{subtitulo}</p>' if subtitulo else ""
    return HTMLResponse(
        "<!doctype html><html lang=es><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(titulo)} · Rory</title><style>{CSS}</style>{extra_head}</head><body>"
        f'<header><div class="bar"><div class="brand"><span>Rory</span> Kicks</div><nav>{nav}</nav></div></header>'
        f"<main><h1>{html.escape(titulo)}</h1>{sub}{cuerpo}</main>{toast}</body></html>"
    )


def _boton(token: str, accion_url: str, campos: dict, texto: str, clase: str = "", volver: str = "") -> str:
    ocultos = "".join(f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(str(v), quote=True)}">' for k, v in campos.items())
    if volver:
        ocultos += f'<input type="hidden" name="volver" value="{html.escape(volver, quote=True)}">'
    return (f'<form class="inl" method="post" action="{accion_url}"><input type="hidden" name="token" value="{html.escape(token, quote=True)}">'
            f'{ocultos}<button class="{clase}">{texto}</button></form>')


def _volver(form, defecto: str, token: str, ok: str = "") -> RedirectResponse:
    destino = str(form.get("volver") or defecto)
    if not destino.startswith("/panel"):
        destino = defecto
    sep = "&" if "?" in destino else "?"
    url = f"{destino}{sep}token={quote(token)}" + (f"&ok={quote(ok)}" if ok else "")
    return RedirectResponse(url, status_code=303)


ICONO_CANAL = {"whatsapp": "💬", "instagram": "📸", "facebook": "👤"}


def _cliente_html(c: dict) -> str:
    tel = c["telefono"]
    if c["plataforma"] == "whatsapp":
        return f'<a href="https://wa.me/{html.escape(tel)}" target="_blank">+{html.escape(tel)}</a>'
    return f'{html.escape(c["plataforma"].capitalize())} ({html.escape(tel.split(":")[-1][-6:])})'


def _estado(c: dict) -> tuple[str, str, str]:
    if c["necesita_persona"]:
        return "persona", "Necesita persona", "bad"
    if c["pausada"]:
        return "pausa", "Rory en pausa", "warn"
    return "rory", "Rory atiende", "ok"


# ── Resumen ──────────────────────────────────────────────────────────────────

@router.get("/panel", response_class=HTMLResponse)
async def panel_resumen(token: str = "", ok: str = ""):
    _exigir(token)
    desde = inicio_de_hoy()
    m = await metricas(desde)
    ultimos = await ultimo_aviso_por_telefono()
    todas = await listar_atencion()
    entregas = await listar_entregas(fecha_hoy_iso())
    nuevas = sum(c["nuevas"] for c in m["por_canal"].values())
    resp = sum(c["respondieron"] for c in m["por_canal"].values())
    pct = round(100 * resp / nuevas) if nuevas else 0
    esp = [c for c in todas if c["necesita_persona"]]
    por_confirmar = [e for e in entregas if e["estado"] == "por_confirmar"]

    stats = (
        f'<div class="grid stats">'
        f'<div class="card stat"><div class="n">{nuevas}</div><div class="t">Conversaciones nuevas hoy</div></div>'
        f'<div class="card stat"><div class="n">{resp} <span class="mut small">({pct}%)</span></div><div class="t">Respondieron al saludo</div></div>'
        f'<div class="card stat"><div class="n">{len(m["avisos"])}</div><div class="t">Pasadas al equipo hoy</div></div>'
        f'<div class="card stat {"alert" if esp else ""}"><div class="n">{len(esp)}</div><div class="t">Esperando a una persona</div></div>'
        f'<div class="card stat"><div class="n">{len(por_confirmar)}</div><div class="t">Entregas por confirmar</div></div></div>'
    )

    tarjetas = ""
    for c in esp:
        u = ultimos.get(c["telefono"], {})
        tipo = u.get("tipo", "otro")
        tarjetas += (
            f'<div class="card"><div><span class="badge {CLASE_TIPO.get(tipo, "info")}">{NOMBRES_TIPO.get(tipo, "🔵 Otro")}</span> '
            f'<span class="mut small">· {esperando_txt(c["ultimo_en"])}</span></div>'
            f'<p style="margin:8px 0 0"><b>{_cliente_html(c)}</b> — {html.escape(c["motivo"])}</p>'
            f'<div class="quote small">“{html.escape(c["texto"])}”</div>'
            f'<div class="acts"><a class="btn" href="/panel/chat?token={quote(token)}&tel={quote(c["telefono"])}">Ver chat</a>'
            + _boton(token, "/panel/accion", {"telefono": c["telefono"], "accion": "atendido"}, "Ya lo atendí", "pri", "/panel")
            + _boton(token, "/panel/accion", {"telefono": c["telefono"], "accion": "reactivar"}, "Reactivar Rory", "", "/panel")
            + "</div></div>"
        )
    esperando = f'<div class="grid">{tarjetas}</div>' if tarjetas else '<div class="card empty">Nadie esperando 🎉</div>'

    proximas = ""
    for e in entregas[:6]:
        proximas += (f'<div class="conv" style="grid-template-columns:1fr auto"><div><b>{fecha_bonita(e["fecha"])}</b> · {html.escape(e["zona"])}<br>'
                     f'<span class="mut small">{html.escape(e["nombre"])} — {html.escape(e["pedido"])}</span></div>'
                     f'<span class="badge {"ok" if e["estado"] == "confirmada" else "warn"}">{"Confirmada" if e["estado"] == "confirmada" else "Por confirmar"}</span></div>')
    proximas = f'<div class="list">{proximas}</div>' if proximas else '<div class="card empty">Sin entregas agendadas. Abre días en <a href="/panel/entregas?token=' + quote(token) + '">Entregas</a>.</div>'

    vacios = [a for a in m["avisos"] if a["tipo"] in ("duda_producto", "fuera_de_catalogo")]
    chips = "".join(f'<li style="margin:4px 0">{NOMBRES_TIPO.get(a["tipo"], "")} — {html.escape(a["motivo"])}</li>' for a in vacios[:25])
    no_supo = f'<div class="card"><ul style="margin:0;padding-left:18px">{chips}</ul></div>' if chips else '<div class="card empty">Rory contestó todo lo que le preguntaron.</div>'

    cuerpo = (stats + f"<h2>Esperando a una persona ({len(esp)})</h2>" + esperando
              + "<h2>Próximas entregas</h2>" + proximas
              + "<h2>Lo que Rory no supo contestar hoy</h2>" + no_supo)
    refresco = "<script>setTimeout(()=>{if(!document.activeElement||document.activeElement===document.body)location.reload()},60000)</script>"
    return pagina(token, "resumen", "Resumen de hoy", cuerpo + refresco, ok, subtitulo=f"{fecha_bonita(fecha_hoy_iso())} · se actualiza solo cada minuto")


# ── Conversaciones ───────────────────────────────────────────────────────────

@router.get("/panel/conversaciones", response_class=HTMLResponse)
async def panel_conversaciones(token: str = "", ok: str = ""):
    _exigir(token)
    todas = await listar_atencion()
    filas = ""
    for c in todas[:300]:
        clave, texto_estado, clase = _estado(c)
        hora = _aware(c["ultimo_en"]).astimezone(zona_mx()).strftime("%d/%m %H:%M")
        filas += (
            f'<div class="conv" data-estado="{clave}" data-q="{html.escape((c["telefono"] + " " + c["texto"]).lower(), quote=True)}">'
            f'<div class="av">{ICONO_CANAL.get(c["plataforma"], "💬")}</div>'
            f'<div><div class="who">{_cliente_html(c)} <span class="badge {clase}">{texto_estado}</span></div>'
            f'<div class="last">{html.escape(c["texto"])}</div><div class="mut small">{hora}</div></div>'
            f'<div class="acts" style="margin:0"><a class="btn" href="/panel/chat?token={quote(token)}&tel={quote(c["telefono"])}">Ver chat</a>'
            + _boton(token, "/panel/accion", {"telefono": c["telefono"], "accion": "pausar"}, "Yo me encargo", "", "/panel/conversaciones")
            + _boton(token, "/panel/accion", {"telefono": c["telefono"], "accion": "reactivar"}, "Reactivar Rory", "", "/panel/conversaciones")
            + "</div></div>"
        )
    lista = f'<div class="list" id="lista">{filas}</div>' if filas else '<div class="card empty">Todavía no hay conversaciones.</div>'
    barra = (
        '<div class="toolbar"><input class="search" type="search" id="q" placeholder="Buscar por número o mensaje…">'
        '<div class="chips" id="filtros"><span class="chip sel" data-f="todas">Todas</span><span class="chip" data-f="persona">Necesitan persona</span>'
        '<span class="chip" data-f="pausa">En pausa</span><span class="chip" data-f="rory">Rory atiende</span></div></div>'
    )
    js = """<script>
let f='todas';const q=document.getElementById('q');
function aplicar(){const t=q.value.toLowerCase();document.querySelectorAll('#lista .conv').forEach(r=>{
 r.style.display=((f==='todas'||r.dataset.estado===f)&&(!t||r.dataset.q.includes(t)))?'':'none'})}
q.addEventListener('input',aplicar);
document.querySelectorAll('#filtros .chip').forEach(c=>c.addEventListener('click',()=>{f=c.dataset.f;
 document.querySelectorAll('#filtros .chip').forEach(x=>x.classList.toggle('sel',x===c));aplicar()}));
</script>"""
    return pagina(token, "conversaciones", "Conversaciones", barra + lista + js, ok,
                  subtitulo="Todos los clientes que han escrito. Pulsa “Ver chat” para leer la conversación completa.")


@router.post("/panel/accion")
async def panel_accion(request: Request):
    form = await request.form()
    token = str(form.get("token", ""))
    _exigir(token)
    tel, accion = str(form.get("telefono", "")), str(form.get("accion", ""))
    if accion == "reactivar":
        await reanudar(tel)
        await marcar_necesita_persona(tel, False)
        msg = "Rory vuelve a atender esta conversación"
    elif accion == "pausar":
        await pausar(tel)
        msg = "Rory en pausa: tú te encargas"
    elif accion == "atendido":
        await marcar_necesita_persona(tel, False)
        msg = "Marcado como atendido"
    else:
        msg = ""
    return _volver(form, "/panel", token, msg)


# ── Chat ─────────────────────────────────────────────────────────────────────

@router.get("/panel/chat", response_class=HTMLResponse)
async def panel_chat(token: str = "", tel: str = "", ok: str = ""):
    _exigir(token)
    msgs = await transcripcion(tel)
    atencion = {c["telefono"]: c for c in await listar_atencion()}
    c = atencion.get(tel)
    estado = ""
    if c:
        _, texto_estado, clase = _estado(c)
        estado = f'<span class="badge {clase}">{texto_estado}</span>'
    modo_g = "tepeaca" if tel.startswith(("ig:", "fb:")) else "puebla"
    burbujas, previo = "", ""
    for x in msgs:
        hora = _aware(x["hora"]).astimezone(zona_mx()).strftime("%d/%m %H:%M")
        corregir = ""
        if x["role"] == "assistant" and previo:
            corregir = (
                f'<details><summary>✏️ Cómo lo habría dicho yo</summary><form method="post" action="/panel/chat/ejemplo">'
                f'<input type="hidden" name="token" value="{html.escape(token, quote=True)}">'
                f'<input type="hidden" name="tel" value="{html.escape(tel, quote=True)}">'
                f'<input type="hidden" name="modo" value="{modo_g}">'
                f'<input type="hidden" name="cliente" value="{html.escape(previo, quote=True)}">'
                f'<textarea name="respuesta" rows="3" placeholder="Escribe cómo lo contestarías tú"></textarea>'
                f'<input type="text" name="nota" placeholder="Nota opcional (ej.: mando primero las fotos del paquete)">'
                f'<button class="pri">Guardar como ejemplo</button></form></details>'
            )
        if x["role"] == "user":
            previo = x["texto"]
        clase = "cli" if x["role"] == "user" else "rory"
        quien = "Cliente" if x["role"] == "user" else "Rory"
        burbujas += f'<div class="b {clase}"><small>{quien} · {hora}</small>{html.escape(x["texto"])}{corregir}</div>'
    burbujas = burbujas or '<div class="card empty">Todavía no hay mensajes guardados de esta conversación.</div>'
    acciones = (
        f'<div class="acts" style="margin:0 0 16px"><a class="btn" href="/panel/conversaciones?token={quote(token)}">← Conversaciones</a>'
        + _boton(token, "/panel/accion", {"telefono": tel, "accion": "pausar"}, "Yo me encargo", "", f"/panel/chat?tel={quote(tel)}")
        + _boton(token, "/panel/accion", {"telefono": tel, "accion": "reactivar"}, "Reactivar Rory", "", f"/panel/chat?tel={quote(tel)}")
        + "</div>"
    )
    titulo = f"+{tel}" if tel.isdigit() else tel
    return pagina(token, "conversaciones", f"Chat {titulo}", acciones + f'<div class="chat">{burbujas}</div>', ok,
                  subtitulo=estado)


@router.post("/panel/chat/ejemplo")
async def panel_chat_ejemplo(request: Request):
    form = await request.form()
    token = str(form.get("token", ""))
    _exigir(token)
    await guardar_ejemplo(str(form.get("modo", "puebla")), str(form.get("cliente", "")),
                          str(form.get("respuesta", "")), str(form.get("nota", "")))
    tel = str(form.get("tel", ""))
    return RedirectResponse(f"/panel/chat?token={quote(token)}&tel={quote(tel)}&ok={quote('Ejemplo guardado: Rory lo usará')}", status_code=303)


# ── Entregas ─────────────────────────────────────────────────────────────────

@router.get("/panel/entregas", response_class=HTMLResponse)
async def panel_entregas(token: str = "", ok: str = ""):
    _exigir(token)
    hoy = fecha_hoy_iso()
    rutas = await listar_rutas(hoy)
    entregas = await listar_entregas(hoy)
    por_fecha: dict[str, list] = {}
    for e in entregas:
        por_fecha.setdefault(e["fecha"], []).append(e)

    dias = ""
    for r in rutas:
        pct = min(100, round(100 * r["ocupados"] / r["cupo"])) if r["cupo"] else 0
        det = ""
        for e in por_fecha.get(r["fecha"], []):
            confirmada = e["estado"] == "confirmada"
            det += (
                f'<div class="ent"><span class="badge {"ok" if confirmada else "warn"}">{"Confirmada" if confirmada else "Por confirmar"}</span>'
                f'<span><b>{html.escape(e["nombre"])}</b> · {html.escape(e["zona"])} · {html.escape(e["pedido"])}</span>'
                f'<a class="btn" href="/panel/chat?token={quote(token)}&tel={quote(e["telefono"])}">Ver chat</a>'
                + ("" if confirmada else _boton(token, "/panel/entrega", {"id": e["id"], "estado": "confirmada"}, "Confirmar", "pri", "/panel/entregas"))
                + _boton(token, "/panel/entrega", {"id": e["id"], "estado": "cancelada"}, "Cancelar", "bad", "/panel/entregas")
                + "</div>"
            )
        zonas = "".join(f'<span class="chip">{html.escape(z)}</span>' for z in r["zonas"])
        dias += (
            f'<div class="card dia"><div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap"><h3>{fecha_bonita(r["fecha"])}</h3>'
            + _boton(token, "/panel/ruta/borrar", {"id": r["id"]}, "Quitar día", "bad", "/panel/entregas") + "</div>"
            f'<div class="chips">{zonas}</div><div class="mut small">🕐 {html.escape(r["horario"] or "Horario por confirmar")} · {r["ocupados"]} de {r["cupo"]} entregas</div>'
            f'<div class="bar-cupo"><i style="width:{pct}%"></i></div>{det}</div>'
        )
    dias = f'<div class="grid">{dias}</div>' if dias else '<div class="card empty">No hay días abiertos. Abre uno abajo y Rory empezará a ofrecerlo a los clientes de esa zona.</div>'

    checks = "".join(f'<label class="chip"><input type="checkbox" name="zonas" value="{z}">{z}</label>' for z in ZONAS_PUEBLA)
    form = (
        f'<div class="card"><form method="post" action="/panel/ruta"><input type="hidden" name="token" value="{html.escape(token, quote=True)}">'
        f'<div class="toolbar"><label>Fecha<br><input type="date" name="fecha" required></label>'
        f'<label>Horario<br><input type="text" name="horario" placeholder="12:00 a 18:00" size="16"></label>'
        f'<label>Entregas que caben<br><input type="number" name="cupo" value="6" min="1" style="width:90px"></label></div>'
        f'<p class="mut small" style="margin:6px 0">Zonas que cubres ese día:</p><div class="chips">{checks}</div>'
        f'<div class="acts"><button class="pri">Abrir día de entregas</button></div></form></div>'
    )
    return pagina(token, "entregas", "Entregas", "<h2 style='margin-top:0'>Días abiertos</h2>" + dias + "<h2>Abrir un día nuevo</h2>" + form, ok,
                  subtitulo="Tú abres los días y las zonas; Rory solo ofrece esos días y agenda las entregas (por confirmar).")


@router.post("/panel/ruta")
async def panel_ruta(request: Request):
    form = await request.form()
    token = str(form.get("token", ""))
    _exigir(token)
    zonas = [z for z in form.getlist("zonas") if z in ZONAS_PUEBLA]
    fecha = str(form.get("fecha", ""))
    msg = "Elige una fecha y al menos una zona"
    if fecha and zonas:
        try:
            date.fromisoformat(fecha)
            await crear_ruta(fecha, zonas, str(form.get("horario", "")), int(form.get("cupo") or 6))
            msg = "Día abierto: Rory ya puede ofrecerlo"
        except ValueError:
            msg = "La fecha o el cupo no son válidos"
    return RedirectResponse(f"/panel/entregas?token={quote(token)}&ok={quote(msg)}", status_code=303)


@router.post("/panel/ruta/borrar")
async def panel_ruta_borrar(request: Request):
    form = await request.form()
    token = str(form.get("token", ""))
    _exigir(token)
    try:
        await borrar_ruta(int(form.get("id") or 0))
    except ValueError:
        pass
    return _volver(form, "/panel/entregas", token, "Día quitado")


@router.post("/panel/entrega")
async def panel_entrega(request: Request):
    form = await request.form()
    token = str(form.get("token", ""))
    _exigir(token)
    estado = str(form.get("estado", ""))
    msg = ""
    if estado in ("confirmada", "cancelada"):
        try:
            await cambiar_estado_entrega(int(form.get("id") or 0), estado)
            msg = "Entrega confirmada" if estado == "confirmada" else "Entrega cancelada: el lugar quedó libre"
        except ValueError:
            pass
    return _volver(form, "/panel/entregas", token, msg)


# ── Practica ─────────────────────────────────────────────────────────────────

JS_PRACTICA = """
const TOKEN=__TOKENJSON__; let hist=[]; let ocupado=false;
function esc(t){return t.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function reiniciar(){hist=[];document.getElementById('chat').innerHTML='';}
async function enviar(){
  if(ocupado)return; const i=document.getElementById('txt'); const texto=i.value.trim(); if(!texto)return; i.value='';
  const chat=document.getElementById('chat'); chat.insertAdjacentHTML('beforeend','<div class="b cli"><small>Cliente</small>'+esc(texto)+'</div>');
  ocupado=true; const esp=document.createElement('div'); esp.className='acc'; esp.textContent='Rory está escribiendo…'; chat.appendChild(esp);
  try{
    const r=await fetch('/panel/practica/mensaje',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:TOKEN,modo:document.getElementById('modo').value,historial:hist,texto:texto})});
    const d=await r.json(); esp.remove();
    (d.acciones||[]).forEach(a=>chat.insertAdjacentHTML('beforeend','<div class="acc">'+esc(a)+'</div>'));
    const id='r'+Date.now();
    chat.insertAdjacentHTML('beforeend','<div class="b rory" id="'+id+'"><small>Rory</small>'+esc(d.respuesta)
      +'<div class="acts"><button onclick="guardar(\\''+id+'\\',true)">👍 Así está bien</button><button onclick="abrir(\\''+id+'\\')">✏️ Así lo diría yo</button></div>'
      +'<div id="'+id+'f" style="display:none;margin-top:8px"><textarea rows="3" placeholder="Escribe cómo lo contestarías tú"></textarea><input type="text" style="width:100%;margin-top:6px" placeholder="Nota opcional (ej.: mando primero las fotos del paquete y luego el precio)"><div class="acts"><button class="pri" onclick="guardar(\\''+id+'\\',false)">Guardar ejemplo</button></div></div></div>');
    const el=document.getElementById(id); el.dataset.cliente=texto; el.dataset.rory=d.respuesta;
    hist.push({role:'user',content:texto}); hist.push({role:'assistant',content:d.respuesta});
    window.scrollTo(0,document.body.scrollHeight);
  }catch(e){esp.textContent='Error: '+e;} ocupado=false;
}
function abrir(id){document.getElementById(id+'f').style.display='block';}
async function guardar(id,bueno){
  const el=document.getElementById(id); let resp=el.dataset.rory, nota='';
  if(!bueno){resp=el.querySelector('textarea').value.trim(); nota=el.querySelector('input').value.trim(); if(!resp){alert('Escribe tu respuesta');return;}}
  await fetch('/panel/practica/ejemplo',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:TOKEN,modo:document.getElementById('modo').value,cliente:el.dataset.cliente,respuesta:resp,nota:nota})});
  el.insertAdjacentHTML('beforeend','<div class="mut small" style="margin-top:6px">✅ Guardado. Rory lo usará como ejemplo.</div>');
}
"""


@router.get("/panel/practica", response_class=HTMLResponse)
async def panel_practica(token: str = "", ok: str = ""):
    _exigir(token)
    ejs = await listar_ejemplos(None, 100)
    lista = "".join(
        f'<div class="card"><span class="badge info">{html.escape(e["modo"])}</span> <b>Cliente:</b> {html.escape(e["cliente"])}'
        f'<div class="quote"><b>Tú:</b> {html.escape(e["respuesta"])}</div>'
        + (f'<div class="mut small" style="margin-top:6px">Nota: {html.escape(e["nota"])}</div>' if e["nota"] else "")
        + _boton(token, "/panel/practica/borrar", {"id": e["id"]}, "Quitar", "bad", "/panel/practica") + "</div>"
        for e in ejs
    ) or '<div class="card empty">Todavía no hay ejemplos. Practica arriba y guarda tus respuestas.</div>'
    cuerpo = (
        '<div class="toolbar"><label>Campaña <select id="modo"><option value="puebla">Puebla (WhatsApp)</option>'
        '<option value="tepeaca">Tepeaca (Instagram/Facebook)</option></select></label>'
        '<button onclick="reiniciar()">Empezar otra conversación</button></div>'
        '<div class="card"><div class="chat" id="chat" style="min-height:120px"></div>'
        '<div class="toolbar" style="margin:12px 0 0"><input class="search" type="text" id="txt" placeholder="Escribe como si fueras el cliente…" onkeydown="if(event.key===\'Enter\')enviar()">'
        '<button class="pri" onclick="enviar()">Enviar</button></div></div>'
        f"<h2>Ejemplos guardados ({len(ejs)})</h2><div class='grid'>{lista}</div>"
        "<script>" + JS_PRACTICA.replace("__TOKENJSON__", json.dumps(token)) + "</script>"
    )
    return pagina(token, "practica", "Practica con Rory", cuerpo, ok,
                  subtitulo="Escribe como cliente: Rory contesta sin mandar nada real. Corrige sus respuestas y las usará de ejemplo.")


@router.post("/panel/practica/mensaje")
async def panel_practica_mensaje(request: Request):
    """Rory contesta como si fuera real, pero sin mandar nada ni tocar clientes."""
    datos = await request.json()
    _exigir(str(datos.get("token", "")))
    modo = "tepeaca" if datos.get("modo") == "tepeaca" else "puebla"
    es_puebla = modo == "puebla"
    historial = [h for h in (datos.get("historial") or []) if h.get("role") in ("user", "assistant")][-20:]
    texto = str(datos.get("texto", ""))[:1000]
    acciones: list[str] = []

    async def foto(producto_id: str, todas: bool = False) -> str:
        p = obtener_producto(producto_id)
        acciones.append(f"📷 Mandaría {'todas las fotos' if todas else 'la foto'} de {p['nombre'] if p else producto_id}")
        return "OK: foto enviada."

    async def fotos_paquete(paquete: str) -> str:
        acciones.append(f"📷 Mandaría una foto de cada juguete del paquete {paquete}")
        return f"OK: fotos del paquete {paquete} enviadas."

    async def catalogo() -> str:
        acciones.append("📎 Mandaría el PDF del catálogo")
        return "OK: catalogo en PDF enviado."

    async def avisar(motivo: str, tipo: str = "otro") -> str:
        acciones.append(f"🔔 Te avisaría ({tipo}): {motivo}")
        return "OK: el equipo quedo avisado. Dile al cliente que un companero le contesta por este chat."

    async def ver_dias(zona: str) -> str:
        dias = await dias_disponibles(zona, fecha_hoy_iso())
        acciones.append(f"📅 Consultaría los días de entrega para {zona}")
        if not dias:
            return "No hay dias de entrega abiertos para esa zona por ahora. Usa avisar_equipo con tipo 'pedido'."
        return "Dias disponibles en " + zona + ":\n" + "\n".join(
            f"- {d['fecha']} ({fecha_bonita(d['fecha'])}), horario {d['horario'] or 'por confirmar'}, quedan {d['quedan']} lugares"
            for d in dias[:5])

    async def agendar(fecha: str, zona: str, nombre: str, pedido: str) -> str:
        acciones.append(f"📅 Agendaría la entrega ({fecha}, {zona}, {nombre}): {pedido}")
        return f"OK: agendada para {fecha} en {zona}. Dile al cliente que quedo agendada y que un companero le confirma la existencia."

    texto_ia = texto
    if es_puebla and not historial:
        acciones.append("📎 Mandaría el PDF de paquetes (automático en el primer mensaje)")
        texto_ia += ("\n\n[Nota interna: el sistema ya le envió el PDF de paquetes en este turno; "
                     "no uses enviar_catalogo.]")
    respuesta, _ = await generar_respuesta(
        texto_ia, historial, foto, catalogo, "whatsapp" if es_puebla else "instagram", modo, avisar,
        fotos_paquete if es_puebla else None, ver_dias if es_puebla else None, agendar if es_puebla else None,
        (await listar_ejemplos(modo, 15))[::-1],
    )
    return {"respuesta": respuesta, "acciones": acciones}


@router.post("/panel/practica/ejemplo")
async def panel_practica_ejemplo(request: Request):
    datos = await request.json()
    _exigir(str(datos.get("token", "")))
    await guardar_ejemplo(str(datos.get("modo", "puebla")), str(datos.get("cliente", "")),
                          str(datos.get("respuesta", "")), str(datos.get("nota", "")))
    return {"ok": True}


@router.post("/panel/practica/borrar")
async def panel_practica_borrar(request: Request):
    form = await request.form()
    token = str(form.get("token", ""))
    _exigir(token)
    try:
        await borrar_ejemplo(int(form.get("id") or 0))
    except ValueError:
        pass
    return _volver(form, "/panel/practica", token, "Ejemplo quitado")
