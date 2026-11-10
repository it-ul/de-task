"""CLI запуска слоя core."""
import argparse
import logging
import uuid

import psycopg
from psycopg.rows import dict_row
from .config import DWH_DSN
from .validate import run as validate_run

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

def main():
    p = argparse.ArgumentParser(description="Загрузка слоя core из raw")
    p.add_argument("--start", required=True, help="начало окна YYYY-MM-DD")
    p.add_argument("--end", required=True, help="конец окна YYYY-MM-DD (включительно)")
    args = p.parse_args()

    with psycopg.connect(DWH_DSN) as conn:
        run_id = uuid.uuid4()
        
        conn.row_factory = dict_row
        conn.execute(
            "INSERT INTO core.etl_runs (run_id, layer, status, start_date, end_date) "
            "VALUES (%s, 'core', 'RUNNING', %s, %s)",
            (run_id, args.start, args.end))
        conn.commit()
        try:
            stats = validate_run(conn, args.start, args.end, run_id)
            conn.execute(
                "UPDATE core.etl_runs SET status='OK', finished_at=now(), "
                "rows_read=%s, rows_loaded=%s, rows_quarantined=%s WHERE run_id=%s",
                (stats["rows_read"], stats["rows_loaded"], stats["rows_quarantined"], run_id))
            conn.commit()
        except Exception:
            conn.execute("UPDATE core.etl_runs SET status='FAILED', finished_at=now() WHERE run_id=%s",
                         (run_id,))
            conn.commit()
            raise

    print(f"run_id: {run_id}")
    for k, v in stats.items():
        print(f"{k}: {v}")

if __name__ == "__main__":
    main()
