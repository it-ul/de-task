"""CLI: python -m etl.run_etl --start 2023-10-01 --end 2026-10-26"""
import argparse
import datetime as dt
import logging

from etl.load_raw import run


def _date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def main() -> None:
    p = argparse.ArgumentParser(description="Загрузка окна в слой raw")
    p.add_argument("--start", type=_date, required=True, help="начало окна (YYYY-MM-DD)")
    p.add_argument("--end", type=_date, required=True, help="конец окна (YYYY-MM-DD)")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    stats = run(args.start, args.end)
    for k, v in stats.items():
        print(f"{k}: {v}")


if __name__ == "__main__":
    main()
