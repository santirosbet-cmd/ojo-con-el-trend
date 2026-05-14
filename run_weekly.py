"""
run_weekly.py — Script maestro semanal (Windows-compatible)
Uso: python run_weekly.py --dropi ruta/dropi.xlsx --meta ruta/meta.csv [--fecha 2026-05-11] [--tasa 21.5] [--notion]
"""

import argparse
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "scripts"))

from db_setup import create_db
from config import DB_PATH, TASA_EUR_MXN


def main():
    parser = argparse.ArgumentParser(description="Pipeline semanal Ojo con el Trend")
    parser.add_argument("--dropi",  required=True, help="Ruta al Excel de Dropi")
    parser.add_argument("--meta",   required=True, help="Ruta al CSV de Meta Ads")
    parser.add_argument("--fecha",  default=date.today().isoformat(), help="YYYY-MM-DD")
    parser.add_argument("--tasa",   type=float, default=TASA_EUR_MXN, help="EUR→MXN")
    parser.add_argument("--notion", action="store_true", help="Publicar en Notion")
    args = parser.parse_args()

    fecha_dt = date.fromisoformat(args.fecha)

    print("=" * 50)
    print("  OJO CON EL TREND — CDO SYSTEM")
    print(f"  Reporte: {args.fecha}")
    print("=" * 50)

    # 1. Asegurar que la DB existe
    create_db()

    # 2. Copiar archivos raw
    dropi_dest = ROOT / "data" / "raw" / "dropi" / f"ordenes_{args.fecha}.xlsx"
    meta_dest  = ROOT / "data" / "raw" / "meta"  / f"meta_{args.fecha}.csv"
    shutil.copy2(args.dropi, dropi_dest)
    shutil.copy2(args.meta,  meta_dest)
    print(f"✅ Archivos copiados a data/raw/")

    # 3. Ingestar
    from ingest import load_dropi, load_meta, calculate_metrics, save_to_db, _print_summary

    print("📥 Cargando archivos...")
    df_d = load_dropi(str(dropi_dest))
    df_m = load_meta(str(meta_dest))

    print("🧮 Calculando métricas...")
    metrics = calculate_metrics(df_d, df_m, args.tasa)

    print("💾 Guardando en base de datos...")
    periodo_inicio = (fecha_dt - timedelta(days=28)).isoformat()
    save_to_db(
        args.fecha, periodo_inicio, args.fecha,
        df_d, df_m, metrics,
    )
    _print_summary(args.fecha, metrics)

    # 4. Notion (opcional)
    if args.notion:
        print("\n📝 Publicando en Notion...")
        try:
            from notion_writer import run as notion_run
            url = notion_run(args.fecha)
            print(f"✅ Notion: {url}")
        except Exception as e:
            print(f"⚠️  Notion falló: {e}")

    print(f"\n✅ Proceso completo para {args.fecha}")
    print(f"   Dashboard: streamlit run dashboard/app.py")


if __name__ == "__main__":
    main()
