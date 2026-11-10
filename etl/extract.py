"""Батчевое чтение первоисточника deals_bigdata (read-only).

Источник открывается в режиме транзакции (server-side cursors), поэтому окно
любого размера не влезает целиком в память. Строки отдаются кортежами ровно в
порядке колонок raw-таблиц — они сразу пригодны для COPY в DWH.
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Any

import psycopg

# Колонки public.prices = колонки raw.prices без служебных (_run_id/_loaded_at)
FACT_COLUMNS: tuple[str, ...] = (
    "id", "created_at", "updated_at", "subscriber_id", "organization_id",
    "loading_date", "loading_city_gcid", "unloading_city_gcid",
    "distance", "total_weight", "total_volume", "body_type", "price",
    "currency_code", "complex", "price_type", "item_id", "is_fact",
    "nearest_rate_date", "meta_item_id", "end_loading_date", "source",
    "external_item_uuid", "outlier_mark", "weight_category",
    "volume_category", "price_group_id",
)

REF_COLUMNS: dict[str, tuple[str, ...]] = {
    "cities": (
        "id", "full_name_local", "full_name_gc", "gc_id", "name",
        "longitude", "latitude", "created_at", "updated_at",
    ),
    "body_types": ("id", "name", '"id_from_1C"', "std_id"),
    "exchange_rates": ("id", "num_code", "value", "setting_date"),
}

CHILD_COLUMNS: dict[str, tuple[str, ...]] = {
    "price_body_types": ("id", "price_id", "body_type_id"),
    "price_dates": ("id", "price_id", "price_date"),
    "price_participants": ("id", "price_id", "subscriber_id"),
}


def _chunks(seq: list[Any], size: int) -> Iterator[list[Any]]:
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def connect_source(dsn: str) -> psycopg.Connection:
    """Соединение с первоисточником (только чтение; в конце делаем rollback)."""
    return psycopg.connect(dsn)


def stream_prices_window(
    src: psycopg.Connection, start: date, end: date, batch_size: int
) -> Iterator[list[tuple]]:
    """Потоковое чтение окна [start, end] по loading_date, упорядочено по id."""
    sql = f"""
        SELECT {", ".join(FACT_COLUMNS)}
        FROM public.prices
        WHERE loading_date BETWEEN %s AND %s
        ORDER BY id
    """
    with src.cursor(name="prices_window") as c:
        c.itersize = batch_size
        c.execute(sql, (start, end))
        while batch := c.fetchmany(batch_size):
            yield batch


def iter_refs(
    src: psycopg.Connection, table: str, cols: tuple[str, ...], batch_size: int
) -> Iterator[list[tuple]]:
    """Полный снимок справочника (таблицы копеечные — читаем целиком)."""
    sql = f"SELECT {', '.join(cols)} FROM public.{table} ORDER BY id"
    with src.cursor(name=f"ref_{table}") as c:
        c.itersize = batch_size
        c.execute(sql)
        while batch := c.fetchmany(batch_size):
            yield batch


def iter_children(
    src: psycopg.Connection,
    table: str,
    cols: tuple[str, ...],
    price_ids: list[int],
    id_chunk: int,
) -> Iterator[list[tuple]]:
    """Дочерние строки по множеству price_id (чанками по id_chunk)."""
    sql = f"SELECT {', '.join(cols)} FROM public.{table} WHERE price_id = ANY(%s) ORDER BY id"
    for n, ids in enumerate(_chunks(price_ids, id_chunk)):
        with src.cursor(name=f"child_{table}_{n}") as c:
            c.itersize = id_chunk
            c.execute(sql, (ids,))
            while batch := c.fetchmany(id_chunk):
                yield batch


def source_count(src: psycopg.Connection, start: date, end: date) -> int:
    """Сколько строк источника попадает в окно (для аудита в load_runs)."""
    with src.cursor() as c:
        c.execute(
            "SELECT count(*) FROM public.prices WHERE loading_date BETWEEN %s AND %s",
            (start, end),
        )
        return c.fetchone()[0]
