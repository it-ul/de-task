"""Слой core: DQ-правила с приоритетом, конвертация валют, нормализация, карантин."""
from psycopg.rows import dict_row
import json
import logging
from datetime import datetime, timezone

import psycopg
from psycopg.types.json import Jsonb

log = logging.getLogger("etl.validate")

RUB = "643"
WEIGHT_BOUNDS = [(30.0, "G20T"), (10.0, "FTL"), (5.0, "10T"), (2.5, "5T"), (0.5, "L5T")]
BODY_GROUP_BY_STD = {56: "Тент", 4: "Реф", 2: "Изотерм", 15: "Борт"}
BODY_PRIORITY = ["Тент", "Реф", "Изотерм", "Борт", "Прочие"]

NORM_COLS = [
    "price_id", "loading_date", "created_at", "subscriber_id", "organization_id",
    "loading_city_id", "unloading_city_id", "loading_city_gcid", "unloading_city_gcid",
    "distance_km", "total_weight", "total_volume", "weight_category", "volume_category",
    "body_type_group", "body_type_ids", "price_src", "currency_code",
    "rate_date", "rate_value", "price_rub", "complex", "price_type",
    "is_fact", "outlier_mark", "source", "run_id",
]
INSERT_NORM = "INSERT INTO core.prices_norm ({}) VALUES ({})".format(
    ", ".join(NORM_COLS), ", ".join(["%s"] * len(NORM_COLS)))
INSERT_QUAR = """
    INSERT INTO core.dq_quarantine_prices (price_id, quarantine_code, reason, price_snapshot, run_id)
    VALUES (%s, %s, %s, %s, %s)
    ON CONFLICT (price_id, quarantine_code) DO NOTHING
"""
INSERT_PRICE_DATES = """
    INSERT INTO core.price_dates (id, price_id, price_date)
    SELECT pd.id, pd.price_id, pd.price_date
    FROM raw.price_dates pd
    JOIN core.prices_norm p ON p.price_id = pd.price_id
    ON CONFLICT (price_id, price_date) DO NOTHING
"""

def weight_category(w):
    w = round(w, 1)
    for bound, cat in WEIGHT_BOUNDS:
        if w >= bound:
            return cat
    return "OTH"

def volume_category(v):
    return "FTL" if v > 0 and v <= 120 else "OTH"

def _snap(row):
    return json.dumps({
        "id": row["id"], "loading_date": str(row["loading_date"]),
        "loading_city_gcid": row["loading_city_gcid"], "unloading_city_gcid": row["unloading_city_gcid"],
        "distance": row["distance"], "total_weight": row["total_weight"],
        "total_volume": row["total_volume"], "price": row["price"],
        "currency_code": row["currency_code"], "nearest_rate_date": str(row["nearest_rate_date"]),
    }, ensure_ascii=False)

def check_row(row, city_map, rates):
    """Возвращает (код, причина) первого сработавшего правила или None."""
    if row["loading_city_gcid"] not in city_map:
        return "UNKNOWN_LOADING_CITY", "loading_city_gcid отсутствует в справочнике городов"
    if row["unloading_city_gcid"] not in city_map:
        return "UNKNOWN_UNLOADING_CITY", "unloading_city_gcid отсутствует в справочнике городов"
    if row["price"] is None or row["price"] <= 0:
        return "NONPOSITIVE_PRICE", f"цена <= 0 ({row['price']})"
    if row["currency_code"] == RUB:
        price_rub = row["price"]
    else:
        rate = rates.get((row["currency_code"], row["nearest_rate_date"]))
        if rate is None:
            return "RATE_NOT_FOUND", "нет курса для валюты на ближайшую дату"
        price_rub = row["price"] * rate
    if price_rub <= 1000:
        return "PRICE_BELOW_RUB_MINIMUM", f"цена в рублях <= 1000 ({round(price_rub, 2)})"
    if row["distance"] is None or row["distance"] <= 0:
        return "NONPOSITIVE_DISTANCE", f"дистанция <= 0 ({row['distance']})"
    if row["total_weight"] is not None and row["total_weight"] < 0:
        return "NEG_WEIGHT", f"отрицательный вес ({row['total_weight']})"
    if row["total_volume"] is not None and row["total_volume"] < 0:
        return "NEG_VOLUME", f"отрицательный объём ({row['total_volume']})"
    return None

def rebuild_cities(conn):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM core.cities_dedup")
        cur.execute("""
            INSERT INTO core.cities_dedup (id, gc_id, name, full_name_local, full_name_gc, longitude, latitude)
            SELECT DISTINCT ON (gc_id) id, gc_id, name, full_name_local, full_name_gc, longitude, latitude
            FROM raw.cities ORDER BY gc_id, id
        """)
        n = cur.rowcount
    log.info("core.cities_dedup: пересобрано %s городов (дедуп по gc_id, побеждает min id)", n)
    return n

def load_price_dates(conn):
    """Перенос помесячной развертки price_dates в core (ТЗ: месяц карточки маршрута).

    Одним батч-SQL, идемпотентно (ON CONFLICT DO NOTHING); только для сделок,
    прошедших валидацию и доехавших до core.prices_norm. Если миграция 005 ещё
    не прогнана — warning и пропуск шага, чтобы не уронить весь пайплайн.
    """
    exists = conn.execute(
        "SELECT to_regclass('core.price_dates') IS NOT NULL AS ok"
    ).fetchone()["ok"]
    if not exists:
        log.warning("core.price_dates не найдена: прогоните миграцию "
                    "sql/migrations/005_core_price_dates.sql, шаг пропущен")
        return 0
    cur = conn.execute(INSERT_PRICE_DATES)
    log.info("core.price_dates: перенесено %s строк развертки", cur.rowcount)
    return cur.rowcount

def load_rates(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT num_code, setting_date, value FROM raw.exchange_rates")
        return {(r["num_code"], r["setting_date"]): r["value"] for r in cur.fetchall()}


def load_body_map(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id, std_id, \"id_from_1C\" FROM raw.body_types")
        return {r["id"]: (r["std_id"] if r["std_id"] is not None else r["id_from_1C"]) for r in cur.fetchall()}

def load_children(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT price_id, body_type_id FROM raw.price_body_types")
        children = {}
        for r in cur.fetchall():
            children.setdefault(r["price_id"], []).append(r["body_type_id"])
        return children

def pick_group(body_ids, body_type, std_of):
    ids = list(body_ids) if body_ids else ([body_type] if body_type is not None else [])
    groups = {BODY_GROUP_BY_STD.get(std_of.get(b), "Прочие") for b in ids}
    for g in BODY_PRIORITY:
        if g in groups:
            return g
    return "Прочие"

def run(conn, start_date, end_date, run_id):
    rebuild_cities(conn)

    with conn.cursor() as cur:
        cur.execute("SELECT id, gc_id FROM core.cities_dedup")
        city_map = {r["gc_id"]: r["id"] for r in cur.fetchall()}
    rates = load_rates(conn)
    std_of = load_body_map(conn)
    children = load_children(conn)

    with conn.cursor() as cur:
        cur.execute("DELETE FROM core.prices_norm WHERE loading_date BETWEEN %s AND %s",
                    (start_date, end_date))
        log.info("core.prices_norm: удалено %s строк окна", cur.rowcount)
        cur.execute("""
            DELETE FROM core.dq_quarantine_prices q
            USING raw.prices p
            WHERE q.price_id = p.id AND p.loading_date BETWEEN %s AND %s
        """, (start_date, end_date))
        log.info("core.dq_quarantine_prices: удалено %s записей окна", cur.rowcount)

    now = datetime.now(timezone.utc)
    read = loaded = quarantined = 0
    buf = {}
    qu_buf = []

    with conn.cursor(name="raw_scan") as scan, conn.cursor() as cur:
        scan.itersize = 10000
        scan.execute("""
            SELECT id, created_at, updated_at, subscriber_id, organization_id, loading_date,
                   loading_city_gcid, unloading_city_gcid, distance, total_weight, total_volume,
                   body_type, price, currency_code, complex, price_type, is_fact,
                   nearest_rate_date, source, outlier_mark
            FROM raw.prices
            WHERE loading_date BETWEEN %s AND %s
            ORDER BY loading_date
        """, (start_date, end_date))

        while True:
            batch = scan.fetchmany(10000)
            if not batch:
                break
            for r in batch:
                read += 1
                bad = check_row(r, city_map, rates)
                if bad:
                    quarantined += 1
                    qu_buf.append((r["id"], bad[0], bad[1], Jsonb(json.loads(_snap(r))), run_id))
                    if len(qu_buf) >= 5000:
                        cur.executemany(INSERT_QUAR, qu_buf)
                        qu_buf.clear()
                    continue

                currency = r["currency_code"]
                if currency == RUB:
                    price_rub, rate_date, rate_value = r["price"], r["nearest_rate_date"], 1.0
                else:
                    rate_date = r["nearest_rate_date"]
                    rate_value = rates[(currency, rate_date)]
                    price_rub = r["price"] * rate_value

                month_key = r["loading_date"].replace(day=1)
                buf.setdefault(month_key, []).append((
                    r["id"], r["loading_date"], r["created_at"], r["subscriber_id"],
                    r["organization_id"], city_map[r["loading_city_gcid"]],
                    city_map[r["unloading_city_gcid"]], r["loading_city_gcid"],
                    r["unloading_city_gcid"], r["distance"], r["total_weight"],
                    r["total_volume"],
                    weight_category(r["total_weight"]), volume_category(r["total_volume"]),
                    pick_group(children.get(r["id"]), r["body_type"], std_of),
                    sorted(children.get(r["id"], [r["body_type"]] if r["body_type"] is not None else [])),
                    r["price"], currency, rate_date, rate_value, round(price_rub, 2),
                    r["complex"], r["price_type"], r["is_fact"], r["outlier_mark"],
                    r["source"], run_id,
                ))
                loaded += 1

            for month_key, rows in list(buf.items()):
                if len(rows) >= 20000:
                    conn.execute("SELECT core.ensure_partition(%s)", (month_key,))
                    cur.executemany(INSERT_NORM, rows)
                    del buf[month_key]

        for month_key, rows in buf.items():
            conn.execute("SELECT core.ensure_partition(%s)", (month_key,))
            cur.executemany(INSERT_NORM, rows)
        if qu_buf:
            cur.executemany(INSERT_QUAR, qu_buf)
            qu_buf.clear()

    # Развертка price_dates переносится после загрузки сделок окна —
    # в той же транзакции (см. оркестратор), идемпотентно.
    rows_price_dates = load_price_dates(conn)

    log.info("core: прочитано %s, загружено %s, откарантинено %s", read, loaded, quarantined)
    return {"rows_read": read, "rows_loaded": loaded, "rows_quarantined": quarantined,
            "rows_price_dates": rows_price_dates}
