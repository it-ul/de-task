"""Идемпотентная загрузка слоя raw в DWH (база dwh, схема raw).

Правила (зафиксированы в README):
1. Справочники (cities, body_types, exchange_rates) — полная перезагрузка:
   TRUNCATE + COPY в одной транзакции.
2. Факт (raw.prices) — по окну loading_date: DELETE окна + COPY, одна транзакция.
3. Дочерние таблицы — DELETE по окну (через JOIN с raw.prices) + COPY
   только для price_id, реально загруженных в этом прогоне.
4. raw.load_runs — реестр прогонов: RUNNING -> OK / FAILED, с счётчиками.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date

import psycopg

from etl.config import BATCH_SIZE, DWH_DSN, ID_CHUNK, SOURCE_DSN
from etl.extract import (
    CHILD_COLUMNS,
    FACT_COLUMNS,
    REF_COLUMNS,
    connect_source,
    iter_children,
    iter_refs,
    source_count,
    stream_prices_window,
)

log = logging.getLogger(__name__)

RUNNING, OK, FAILED = "RUNNING", "OK", "FAILED"


def _copy_rows(copy, rows: list[tuple], run_id: uuid.UUID | None = None) -> None:
    for r in rows:
        copy.write_row(r + (run_id,) if run_id is not None else r)


def load_references(src: psycopg.Connection, dwh: psycopg.Connection) -> dict[str, int]:
    """Полная перезагрузка справочников: TRUNCATE + COPY за одну транзакцию."""
    stats: dict[str, int] = {}
    with dwh.transaction():
        with dwh.cursor() as cur:
            cur.execute("TRUNCATE raw.cities, raw.body_types, raw.exchange_rates")
        for table, cols in REF_COLUMNS.items():
            n = 0
            with dwh.cursor() as cur, cur.copy(
                f"COPY raw.{table} ({', '.join(cols)}) FROM STDIN"
            ) as copy:
                for rows in iter_refs(src, table, cols, BATCH_SIZE):
                    _copy_rows(copy, rows)
                    n += len(rows)
            stats[table] = n
            log.info("refs.%s: загружено %d строк", table, n)
    return stats


def load_prices(
    src: psycopg.Connection,
    dwh: psycopg.Connection,
    run_id: uuid.UUID,
    start: date,
    end: date,
) -> tuple[int, int]:
    """Факт: DELETE окна + COPY. Возврат (удалено, загружено)."""
    with dwh.transaction():
        with dwh.cursor() as cur:
            cur.execute(
                "DELETE FROM raw.prices WHERE loading_date BETWEEN %s AND %s",
                (start, end),
            )
            deleted = cur.rowcount
        copied = 0
        with dwh.cursor() as cur, cur.copy(
            f"COPY raw.prices ({', '.join(FACT_COLUMNS)}, _run_id) FROM STDIN"
        ) as copy:
            for rows in stream_prices_window(src, start, end, BATCH_SIZE):
                _copy_rows(copy, rows, run_id)
                copied += len(rows)
    log.info("raw.prices: удалено %d, загружено %d", deleted, copied)
    return deleted, copied


def load_children(
    src: psycopg.Connection,
    dwh: psycopg.Connection,
    run_id: uuid.UUID,
    start: date,
    end: date,
) -> dict[str, int]:
    """Дочерние таблицы: DELETE по окну + COPY по id загруженного факта."""
    stats: dict[str, int] = {}
    with dwh.transaction():
        with dwh.cursor() as cur:
            cur.execute(
                "SELECT id FROM raw.prices WHERE loading_date BETWEEN %s AND %s ORDER BY id",
                (start, end),
            )
            price_ids = [r[0] for r in cur.fetchall()]

        for table, cols in CHILD_COLUMNS.items():
            if not price_ids:
                stats[table] = 0
                continue
            with dwh.cursor() as cur:
                cur.execute(
                    f"DELETE FROM raw.{table} b USING raw.prices p "
                    "WHERE b.price_id = p.id AND p.loading_date BETWEEN %s AND %s",
                    (start, end),
                )
            n = 0
            with dwh.cursor() as cur, cur.copy(
                f"COPY raw.{table} ({', '.join(cols)}, _run_id) FROM STDIN"
            ) as copy:
                for rows in iter_children(src, table, cols, price_ids, ID_CHUNK):
                    _copy_rows(copy, rows, run_id)
                    n += len(rows)
            stats[table] = n
            log.info("raw.%s: загружено %d строк", table, n)
    return stats


def run(start: date, end: date) -> dict:
    """Полный прогон raw-загрузки за окно дат. Идемпотентен."""
    run_id = uuid.uuid4()
    src = connect_source(SOURCE_DSN)
    dwh = psycopg.connect(DWH_DSN, autocommit=True)  # реестр живёт вне транзакций данных
    stats: dict = {}
    try:
        with dwh.cursor() as cur:
            cur.execute(
                "INSERT INTO raw.load_runs (run_id, start_date, end_date, status) "
                "VALUES (%s, %s, %s, %s)",
                (run_id, start, end, RUNNING),
            )
        try:
            stats |= {f"refs.{k}": v for k, v in load_references(src, dwh).items()}
            stats["rows_source"] = source_count(src, start, end)
            _, loaded = load_prices(src, dwh, run_id, start, end)
            stats["rows_loaded"] = loaded
            stats |= {f"children.{k}": v for k, v in load_children(src, dwh, run_id, start, end).items()}

            with dwh.cursor() as cur:
                cur.execute(
                    "UPDATE raw.load_runs SET finished_at = now(), status = %s, "
                    "rows_source = %s, rows_loaded = %s, details = %s WHERE run_id = %s",
                    (OK, stats["rows_source"], stats["rows_loaded"],
                     psycopg.types.json.Json(stats), run_id),
                )
        except Exception:
            with dwh.cursor() as cur:
                cur.execute(
                    "UPDATE raw.load_runs SET finished_at = now(), status = %s WHERE run_id = %s",
                    (FAILED, run_id),
                )
            raise
        return stats
    finally:
        src.rollback()   # источник read-only: закрываем транзакцию чтения без записи
        src.close()
        dwh.close()
