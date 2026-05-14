"""
notion_writer.py — Escribe el reporte semanal en Notion
Uso: python scripts/notion_writer.py --fecha 2026-05-11
"""

import sqlite3
import os
import argparse
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))
from config import DB_PATH, BENCHMARKS

try:
    from notion_client import Client
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    print("❌ Instala: pip install notion-client python-dotenv")
    sys.exit(1)


def _notion_client():
    token = os.getenv("NOTION_TOKEN")
    if not token:
        raise EnvironmentError("Falta NOTION_TOKEN en el archivo .env")
    return Client(auth=token)


def _parent_id():
    pid = os.getenv("NOTION_PARENT_PAGE_ID")
    if not pid:
        raise EnvironmentError("Falta NOTION_PARENT_PAGE_ID en el archivo .env")
    return pid


# ── HELPERS ───────────────────────────────────────────────────────────────

def sem(val, good, bad, higher_is_better=True) -> str:
    if higher_is_better:
        return "🟢" if val >= good else ("🟡" if val >= bad else "🔴")
    else:
        return "🟢" if val <= good else ("🟡" if val <= bad else "🔴")


def fmt_mxn(val) -> str:
    return f"${val:,.2f} MXN"

def fmt_pct(val) -> str:
    return f"{val:.1f}%"

def txt(s: str) -> list:
    return [{"type": "text", "text": {"content": str(s)}}]

def h2(content: str) -> dict:
    return {"type": "heading_2", "heading_2": {"rich_text": txt(content)}}

def divider() -> dict:
    return {"type": "divider", "divider": {}}

def table(headers: list, rows: list) -> dict:
    width = len(headers)
    children = [{"type": "table_row", "table_row": {"cells": [txt(h) for h in headers]}}]
    for row in rows:
        children.append({"type": "table_row", "table_row": {"cells": [txt(str(c)) for c in row]}})
    return {"type": "table", "table": {
        "table_width": width,
        "has_column_header": True,
        "has_row_header": False,
        "children": children,
    }}


# ── DATOS DESDE SQLITE ────────────────────────────────────────────────────

def get_week_data(fecha_reporte: str, db_path=None) -> dict:
    path = str(db_path or DB_PATH)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute("SELECT id FROM semanas WHERE fecha_reporte = ?", (fecha_reporte,))
    row = c.fetchone()
    if not row:
        raise ValueError(f"No hay datos para {fecha_reporte}. Ejecuta primero ingest.py.")
    semana_id = row["id"]

    c.execute("SELECT * FROM metricas_semanales WHERE semana_id = ?", (semana_id,))
    metrics = dict(c.fetchone())

    c.execute(
        "SELECT * FROM transportadoras_semanales WHERE semana_id = ? ORDER BY total DESC",
        (semana_id,),
    )
    transp = [dict(r) for r in c.fetchall()]

    c.execute(
        """SELECT campana,
                  SUM(gasto_eur)    AS gasto,
                  SUM(compras_meta) AS compras,
                  AVG(roas_meta)    AS roas,
                  SUM(clics)        AS clics
           FROM meta_ads_diario WHERE semana_id = ?
           GROUP BY campana ORDER BY gasto DESC""",
        (semana_id,),
    )
    camps = [dict(r) for r in c.fetchall()]

    c.execute(
        """SELECT s.fecha_reporte, m.ventas_confirmadas, m.utilidad_neta,
                  m.mer, m.cpa_real, m.roas_real, m.tasa_cancelacion, m.pct_entrega
           FROM semanas s JOIN metricas_semanales m ON s.id = m.semana_id
           ORDER BY s.fecha_reporte DESC LIMIT 8""",
    )
    historico = [dict(r) for r in c.fetchall()]

    conn.close()
    return {"semana_id": semana_id, "metrics": metrics,
            "transportadoras": transp, "campanas": camps, "historico": historico}


# ── CONSTRUCCIÓN DE BLOQUES ───────────────────────────────────────────────

def build_blocks(fecha: str, data: dict) -> list:
    m = data["metrics"]
    B = BENCHMARKS

    estado = ("🔴 EMERGENCIA" if m["utilidad_neta"] < -5000
              else "🟡 EN RIESGO" if m["utilidad_neta"] < 0
              else "🟢 RENTABLE")
    mer_ok = m["mer"] >= B["mer_peligro"]

    blocks = [
        {"type": "callout", "callout": {
            "rich_text": txt(
                f"{estado} | Utilidad: {fmt_mxn(m['utilidad_neta'])} | "
                f"MER: {m['mer']:.2f} {'✅' if mer_ok else '⚠️ DINERO EN PELIGRO'} | "
                f"CPA: {fmt_mxn(m['cpa_real'])} (BE: {fmt_mxn(m['cpa_breakeven'])})"
            ),
            "icon": {"emoji": "⚡"},
            "color": "red_background" if m["utilidad_neta"] < 0 else "green_background",
        }},
        divider(),

        # P&L
        h2("💰 Profit & Loss"),
        table(
            ["Concepto", "Valor", "Nota"],
            [
                ("Revenue confirmado",   fmt_mxn(m["ventas_confirmadas"]),  ""),
                ("  − COGS (producto)",  fmt_mxn(-m["cogs"]),               ""),
                ("  − Flete entregadas", fmt_mxn(-m["flete_total"]),        ""),
                ("= Margen Bruto",       fmt_mxn(m["margen_bruto"]),        fmt_pct(m["margen_bruto_pct"])),
                ("  − Meta Ads",         fmt_mxn(-m["gasto_ads_mxn"]),      f"€{m['gasto_ads_eur']:.2f}"),
                ("= Utilidad Neta",      fmt_mxn(m["utilidad_neta"]),       fmt_pct(m["utilidad_neta_pct"])),
                ("", "", ""),
                ("Pipeline (en tránsito)", fmt_mxn(m["ventas_pipeline"]),   "no cobrado aún"),
                ("Devoluciones (costo hundido)", fmt_mxn(-m["costo_devoluciones"]), ""),
                ("MER",                  f"{m['mer']:.2f}",                 "✅ OK" if mer_ok else "⚠️ PELIGRO"),
                ("Días de pauta rest.",  f"{m['dias_pauta_restantes']:.1f}","⚠️ ALARMA" if m["dias_pauta_restantes"] < B["dias_pauta_alerta"] else ""),
            ],
        ),
        divider(),

        # KPIs
        h2("🎯 KPIs Clave"),
        table(
            ["KPI", "Real", "Referencia", "Benchmark", "Estado"],
            [
                ("ROAS Real",           f"{m['roas_real']:.2f}×",
                                        f"{m['roas_meta_promedio']:.2f}× Meta",
                                        f">{B['roas_real_objetivo']}×",
                                        sem(m["roas_real"], B["roas_real_objetivo"], B["roas_real_min"])),
                ("CPA Real",            fmt_mxn(m["cpa_real"]),
                                        f"BE: {fmt_mxn(m['cpa_breakeven'])}",
                                        "< Break-even",
                                        sem(m["cpa_real"], m["cpa_breakeven"]*0.8, m["cpa_breakeven"], False)),
                ("AOV",                 fmt_mxn(m["aov"]),            "", f">${B['aov_objetivo']:,}",
                                        sem(m["aov"], B["aov_objetivo"], B["aov_objetivo"]*0.8)),
                ("Tasa Cancelación",    fmt_pct(m["tasa_cancelacion"]), "", f"<{B['cancelacion_max']}%",
                                        sem(m["tasa_cancelacion"], 10, B["cancelacion_max"], False)),
                ("% Entrega Efectiva",  fmt_pct(m["pct_entrega"]),     "", f">{B['entrega_min']}%",
                                        sem(m["pct_entrega"], B["entrega_min"], 40)),
                ("CTR",                 fmt_pct(m["ctr_promedio"]),    "", f">{B['ctr_min']}%",
                                        sem(m["ctr_promedio"], 3, B["ctr_min"])),
                ("CPC",                 f"€{m['cpc_promedio']:.3f}",  "", "Bajo",
                                        "🟢" if m["cpc_promedio"] < 0.5 else "🟡"),
                ("MER",                 f"{m['mer']:.2f}",            "", f">{B['mer_peligro']}",
                                        sem(m["mer"], B["mer_objetivo"], B["mer_peligro"])),
            ],
        ),
        divider(),

        # Logística
        h2("📦 Logística Dropi"),
        table(
            ["Carrier", "Total", "Entregadas", "Canceladas", "Novedades", "% Entrega"],
            [
                (t["transportadora"], t["total"], t["entregadas"],
                 t["canceladas"], t["novedades"], f"{t['pct_entrega']:.1f}%")
                for t in data["transportadoras"]
            ] or [("Sin datos", "—", "—", "—", "—", "—")],
        ),
        divider(),
    ]

    # Campañas Meta
    if data["campanas"]:
        blocks += [
            h2("📣 Campañas Meta Ads"),
            table(
                ["Campaña", "Gasto (€)", "Compras", "ROAS Meta", "Clics"],
                [
                    (
                        (c["campana"] or "")[:50],
                        f"€{c['gasto']:.2f}",
                        int(c["compras"] or 0),
                        f"{c['roas']:.2f}×" if c["roas"] else "—",
                        int(c["clics"] or 0),
                    )
                    for c in data["campanas"]
                ],
            ),
            divider(),
        ]

    # Histórico
    if len(data["historico"]) > 1:
        blocks += [
            h2("📈 Tendencia (últimas semanas)"),
            table(
                ["Semana", "Ventas", "Utilidad", "MER", "CPA", "ROAS"],
                [
                    (
                        h["fecha_reporte"],
                        fmt_mxn(h["ventas_confirmadas"]),
                        fmt_mxn(h["utilidad_neta"]),
                        f"{h['mer']:.2f}",
                        fmt_mxn(h["cpa_real"]),
                        f"{h['roas_real']:.2f}×",
                    )
                    for h in data["historico"]
                ],
            ),
            divider(),
        ]

    blocks.append({"type": "paragraph", "paragraph": {
        "rich_text": [{"type": "text", "text": {
            "content": f"Generado automáticamente · {fecha} · Ojo con el Trend CDO System"
        }, "annotations": {"color": "gray"}}]
    }})

    return blocks


# ── PUBLICAR EN NOTION ────────────────────────────────────────────────────

def create_notion_page(fecha: str, data: dict) -> str:
    notion    = _notion_client()
    parent_id = _parent_id()
    m         = data["metrics"]

    util_emoji = "🔴" if m["utilidad_neta"] < 0 else "🟢"
    title = (
        f"{util_emoji} Reporte · {fecha} · "
        f"MER {m['mer']:.2f} · "
        f"Utilidad {fmt_mxn(m['utilidad_neta'])}"
    )

    blocks = build_blocks(fecha, data)

    page = notion.pages.create(
        parent={"page_id": parent_id},
        properties={"title": {"title": txt(title)}},
        children=blocks[:100],
    )
    page_id = page["id"]

    for i in range(100, len(blocks), 100):
        notion.blocks.children.append(page_id, children=blocks[i : i + 100])

    url = f"https://www.notion.so/{page_id.replace('-', '')}"
    print(f"✅ Página creada: {url}")
    return url


def run(fecha: str, db_path=None) -> str:
    print(f"📊 Generando reporte Notion para {fecha}...")
    data = get_week_data(fecha, db_path)
    return create_notion_page(fecha, data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fecha", required=True, help="YYYY-MM-DD")
    parser.add_argument("--db", default=None)
    args = parser.parse_args()
    run(args.fecha, args.db)
