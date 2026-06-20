"""
Superset auto-initialization — v3.
Fixes: pie orderby, table ordering, adds visual analytics charts.
"""
import json
import logging
import sqlite3
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("superset_init")

CH_URI = "clickhousedb+connect://bank:bank_pass@mart-clickhouse:8123/bank_marts"
CH_RAW_URI = "clickhousedb+connect://bank:bank_pass@processor-clickhouse:8123/bank_raw"
DB_PATH = "/app/superset_home/superset.db"


def wait_for_superset(retries=30, delay=5):
    import requests
    for i in range(1, retries + 1):
        try:
            r = requests.get("http://localhost:8088/health", timeout=5)
            if r.status_code == 200:
                log.info("Superset ready.")
                return True
        except Exception:
            pass
        log.info("Waiting... (%d/%d)", i, retries)
        time.sleep(delay)
    return False


def api_session():
    import requests
    s = requests.Session()
    r = s.get("http://localhost:8088/login/", timeout=10)
    csrf = r.cookies.get("csrf_token", "")
    s.post("http://localhost:8088/login/", data={"username": "admin", "password": "admin", "csrf_token": csrf}, allow_redirects=False)
    return s


def api_post(s, path, data):
    r = s.post(f"http://localhost:8088/api/v1{path}", json=data, headers={"Content-Type": "application/json"}, timeout=30)
    if r.status_code not in (200, 201):
        log.warning("POST %s -> %d: %s", path, r.status_code, r.text[:200])
    return r


def get_sqlite():
    return sqlite3.connect(DB_PATH)


def clean_all():
    conn = get_sqlite()
    cur = conn.cursor()
    cur.execute("DELETE FROM dashboard_slices")
    cur.execute("DELETE FROM slices")
    cur.execute("DELETE FROM tables")
    cur.execute("DELETE FROM dbs")
    conn.commit()
    conn.close()
    log.info("Cleaned all.")


def setup_databases(s):
    r = api_post(s, "/database/", {"database_name": "ClickHouse Marts", "sqlalchemy_uri": CH_URI, "expose_in_sqllab": True, "allow_dml": True, "allow_run_async": True, "extra": '{"allows_virtual_table_explore": true}'})
    marts_db = r.json()["id"] if r.status_code in (200, 201) else None

    r = api_post(s, "/database/", {"database_name": "ClickHouse Raw", "sqlalchemy_uri": CH_RAW_URI, "expose_in_sqllab": True, "allow_dml": True, "allow_run_async": True, "extra": '{"allows_virtual_table_explore": true}'})
    raw_db = r.json()["id"] if r.status_code in (200, 201) else None

    log.info("DBs: marts=%s raw=%s", marts_db, raw_db)
    return marts_db, raw_db


def create_dataset(s, db_id, name, sql):
    r = api_post(s, "/dataset/", {"database": db_id, "table_name": name, "schema": "bank_marts", "sql": sql})
    if r.status_code in (200, 201):
        return r.json()["id"]
    log.warning("Failed dataset '%s': %s", name, r.text[:150])
    return None


def setup_datasets(marts_db, raw_db):
    s = api_session()
    ds = {}
    for name, sql in {
        "daily_turnover": "SELECT t.*, b.company_name, b.industry, b.segment, b.region FROM daily_turnover t LEFT JOIN dim_businesses b ON t.business_id = b.business_id",
        "monthly_turnover": "SELECT t.*, b.company_name, b.industry, b.segment, b.region FROM monthly_turnover t LEFT JOIN dim_businesses b ON t.business_id = b.business_id",
        "daily_service_usage": "SELECT s.*, c.full_name AS client_name, sv.service_name, sv.service_type FROM daily_service_usage s LEFT JOIN dim_clients c ON s.client_id = c.client_id LEFT JOIN dim_services sv ON s.service_id = sv.service_id",
        "monthly_service_usage": "SELECT s.*, c.full_name AS client_name, sv.service_name, sv.service_type FROM monthly_service_usage s LEFT JOIN dim_clients c ON s.client_id = c.client_id LEFT JOIN dim_services sv ON s.service_id = sv.service_id",
        "daily_friction_stats": "SELECT f.*, c.full_name AS client_name FROM daily_friction_stats f LEFT JOIN dim_clients c ON f.client_id = c.client_id",
        "anomaly_alerts": "SELECT a.*, CASE WHEN a.entity_type='business' THEN b.company_name WHEN a.entity_type='client' THEN c.full_name ELSE NULL END AS entity_name FROM anomaly_alerts a LEFT JOIN dim_businesses b ON a.entity_id = b.business_id AND a.entity_type='business' LEFT JOIN dim_clients c ON a.entity_id = c.client_id AND a.entity_type='client'",
        "dim_businesses": "SELECT * FROM dim_businesses",
        "dim_clients": "SELECT * FROM dim_clients",
        "dim_services": "SELECT * FROM dim_services",
        "dim_funnels": "SELECT * FROM dim_funnels",
        "client_service_baseline": "SELECT b.*, c.full_name AS client_name FROM client_service_baseline b LEFT JOIN dim_clients c ON b.client_id = c.client_id",
        "client_friction_baseline": "SELECT b.*, c.full_name AS client_name FROM client_friction_baseline b LEFT JOIN dim_clients c ON b.client_id = c.client_id",
    }.items():
        did = create_dataset(s, marts_db, name, sql)
        if did: ds[name] = did

    for name, sql in {
        "raw_ux_events": "SELECT * FROM ux_events",
        "raw_sessions": "SELECT * FROM sessions",
        "raw_support_tickets": "SELECT * FROM support_tickets",
    }.items():
        did = create_dataset(s, raw_db, name, sql)
        if did: ds[name] = did

    log.info("Datasets: %d", len(ds))
    return ds


def ch(s, name, viz, ds_id, params):
    """Create chart, return id or None."""
    r = api_post(s, "/chart/", {"slice_name": name, "viz_type": viz, "datasource_id": ds_id, "datasource_type": "table", "params": json.dumps(params)})
    return r.json()["id"] if r.status_code in (200, 201) else None


def _pos(title, chart_ids):
    pos = {"DASHBOARD_VERSION_KEY": "v2", "ROOT_ID": {"type": "ROOT", "id": "ROOT_ID", "children": ["GRID_ID"]}, "GRID_ID": {"type": "GRID", "id": "GRID_ID", "children": [], "parents": ["ROOT_ID"]}, "HEADER_ID": {"type": "HEADER", "id": "HEADER_ID", "meta": {"text": title}}}
    ri = 0
    for i, cid in enumerate(chart_ids):
        if i % 3 == 0:
            rid = f"ROW-{ri}"; ri += 1
            pos["GRID_ID"]["children"].append(rid)
            pos[rid] = {"type": "ROW", "id": rid, "children": [], "parents": ["ROOT_ID", "GRID_ID"], "meta": {"background": "BACKGROUND_TRANSPARENT"}}
        ck = f"CHART-{cid}"
        pos[rid]["children"].append(ck)
        pos[ck] = {"type": "CHART", "id": ck, "children": [], "parents": ["ROOT_ID", "GRID_ID", rid], "meta": {"width": 4, "height": 50, "chartId": cid}}
    return json.dumps(pos)


def save_dash(title, slug, chart_ids):
    from datetime import datetime
    conn = get_sqlite(); cur = conn.cursor()
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    p = _pos(title, chart_ids)
    row = cur.execute("SELECT id FROM dashboards WHERE slug=?", (slug,)).fetchone()
    if row:
        cur.execute("UPDATE dashboards SET dashboard_title=?, position_json=?, published=1 WHERE id=?", (title, p, row[0]))
        did = row[0]
    else:
        cur.execute("INSERT INTO dashboards (dashboard_title, slug, published, position_json, created_on, changed_on) VALUES (?, ?, 1, ?, ?, ?)", (title, slug, p, now, now))
        did = cur.execute("SELECT id FROM dashboards WHERE slug=?", (slug,)).fetchone()[0]
    for cid in chart_ids:
        cur.execute("INSERT OR IGNORE INTO dashboard_slices (dashboard_id, slice_id) VALUES (?, ?)", (did, cid))
    conn.commit(); conn.close()
    log.info("Dashboard '%s': %d charts", title, len(chart_ids))


def setup_dashboards(ds):
    s = api_session()

    # ================================================================
    # ДАШБОРД 1: ОБОРОТЫ МСБ
    # ================================================================
    C = []
    dt = ds.get("daily_turnover")
    mt = ds.get("monthly_turnover")
    db = ds.get("dim_businesses")
    dc = ds.get("dim_clients")
    aa = ds.get("anomaly_alerts")

    # --- Визуальные графики ---
    if dt:
        # Общий приток и отток по дням (без разбивки по бизнесам)
        c = ch(s, "Дневной приток/отток", "echarts_area", dt,
               {"x_axis": "date", "time_grain_sqla": "P1D",
                "metrics": [{"expressionType": "SQL", "sqlExpression": "sum(inflow_sum)", "label": "Приток", "expressionType": "SQL"},
                            {"expressionType": "SQL", "sqlExpression": "sum(outflow_sum)", "label": "Отток", "expressionType": "SQL"}],
                "groupby": [], "row_limit": 10000, "show_legend": True, "rich_tooltip": True})
        if c: C.append(c)

        # Оборот по регионам (bar chart)
        c = ch(s, "Оборот по регионам", "echarts_timeseries_bar", dt,
               {"x_axis": "region", "metrics": [{"expressionType": "SQL", "sqlExpression": "sum(inflow_sum)", "label": "Приток", "expressionType": "SQL"}],
                "groupby": [], "row_limit": 20, "show_legend": True})
        if c: C.append(c)

        # Оборот по отраслям (horizontal bar)
        c = ch(s, "Оборот по отраслям", "echarts_timeseries_bar", dt,
               {"x_axis": "industry", "metrics": [{"expressionType": "SQL", "sqlExpression": "sum(inflow_sum)", "label": "Приток", "expressionType": "SQL"}],
                "groupby": [], "row_limit": 20, "show_legend": True})
        if c: C.append(c)

    if mt:
        # Топ-10 бизнесов по месячному обороту
        c = ch(s, "Топ-10 бизнесов по обороту", "echarts_timeseries_bar", mt,
               {"x_axis": "company_name", "metrics": [{"expressionType": "SQL", "sqlExpression": "sum(inflow_sum)", "label": "Оборот", "expressionType": "SQL"}],
                "groupby": [], "row_limit": 10, "show_legend": True})
        if c: C.append(c)

    if db:
        # Бизнесы по сегменту (pie)
        c = ch(s, "Бизнесы по сегменту", "pie", db,
               {"groupby": ["segment"], "metric": {"expressionType": "SQL", "sqlExpression": "count()", "label": "count", "expressionType": "SQL"},
                "row_limit": 100, "show_labels": True, "show_legend": True, "sort_by_metric": True})
        if c: C.append(c)

        # Бизнесы по отрасли (pie)
        c = ch(s, "Бизнесы по отрасли", "pie", db,
               {"groupby": ["industry"], "metric": {"expressionType": "SQL", "sqlExpression": "count()", "label": "count", "expressionType": "SQL"},
                "row_limit": 20, "show_labels": True, "show_legend": True, "sort_by_metric": True})
        if c: C.append(c)

        # Бизнесы по региону (pie)
        c = ch(s, "Бизнесы по региону", "pie", db,
               {"groupby": ["region"], "metric": {"expressionType": "SQL", "sqlExpression": "count()", "label": "count", "expressionType": "SQL"},
                "row_limit": 20, "show_labels": True, "show_legend": True, "sort_by_metric": True})
        if c: C.append(c)

    # --- KPI ---
    if dt:
        c = ch(s, "KPI: Оборот сегодня", "big_number_total", dt,
               {"metric": {"expressionType": "SQL", "sqlExpression": "sum(inflow_sum)", "label": "Приток", "expressionType": "SQL"}, "y_axis_format": ",.0f"})
        if c: C.append(c)

    if aa:
        # Аномалии оборотов: количество по severity (bar)
        c = ch(s, "Аномалии оборотов по критичности", "echarts_timeseries_bar", aa,
               {"x_axis": "severity", "metrics": [{"expressionType": "SQL", "sqlExpression": "count()", "label": "Аномалии", "expressionType": "SQL"}],
                "groupby": [], "row_limit": 10, "show_legend": True,
                "adhoc_filters": [{"clause": "WHERE", "expressionType": "SIMPLE", "subject": "anomaly_type", "operator": "==", "comparator": "turnover_anomaly", "filterOptionName": "f1"}]})
        if c: C.append(c)

        # Аномалии оборотов по дням (line)
        c = ch(s, "Аномалии оборотов по дням", "echarts_timeseries_line", aa,
               {"x_axis": "detected_at", "time_grain_sqla": "P1D",
                "metrics": [{"expressionType": "SQL", "sqlExpression": "count()", "label": "Аномалии", "expressionType": "SQL"}],
                "groupby": ["severity"], "row_limit": 1000, "show_legend": True,
                "adhoc_filters": [{"clause": "WHERE", "expressionType": "SIMPLE", "subject": "anomaly_type", "operator": "==", "comparator": "turnover_anomaly", "filterOptionName": "f1b"}]})
        if c: C.append(c)

    save_dash("Обороты МСБ", "executive-overview", C)

    # ================================================================
    # ДАШБОРД 2: ИСПОЛЬЗОВАНИЕ СЕРВИСОВ
    # ================================================================
    C = []
    dsu = ds.get("daily_service_usage")
    msu = ds.get("monthly_service_usage")

    if dsu:
        # Использование сервисов по дням (area)
        c = ch(s, "Сессии по сервисам по дням", "echarts_area", dsu,
               {"x_axis": "date", "time_grain_sqla": "P1D",
                "metrics": [{"expressionType": "SQL", "sqlExpression": "sum(session_count)", "label": "Сессии", "expressionType": "SQL"}],
                "groupby": ["service_name"], "row_limit": 10000, "show_legend": True})
        if c: C.append(c)

        # Покупки по сервисам по дням (area)
        c = ch(s, "Покупки по сервисам по дням", "echarts_area", dsu,
               {"x_axis": "date", "time_grain_sqla": "P1D",
                "metrics": [{"expressionType": "SQL", "sqlExpression": "sum(tx_sum)", "label": "Сумма покупок", "expressionType": "SQL"}],
                "groupby": ["service_name"], "row_limit": 10000, "show_legend": True})
        if c: C.append(c)

        # Топ сервисов по сессиям
        c = ch(s, "Сервисы по числу сессий", "echarts_timeseries_bar", dsu,
               {"x_axis": "service_name", "metrics": [{"expressionType": "SQL", "sqlExpression": "sum(session_count)", "label": "Сессии", "expressionType": "SQL"}],
                "groupby": [], "row_limit": 20, "show_legend": True})
        if c: C.append(c)

        # Топ сервисов по отменам
        c = ch(s, "Сервисы по отменам", "echarts_timeseries_bar", dsu,
               {"x_axis": "service_name", "metrics": [{"expressionType": "SQL", "sqlExpression": "sum(cancel_count)", "label": "Отмены", "expressionType": "SQL"}],
                "groupby": [], "row_limit": 20, "show_legend": True})
        if c: C.append(c)

        # Конверсия: покупки / сессии по сервисам
        c = ch(s, "Конверсия в покупку по сервисам", "echarts_timeseries_bar", dsu,
               {"x_axis": "service_name",
                "metrics": [{"expressionType": "SQL", "sqlExpression": "round(sum(tx_count)/greatest(sum(session_count),1)*100,1)", "label": "Конверсия %", "expressionType": "SQL"}],
                "groupby": [], "row_limit": 20, "show_legend": True})
        if c: C.append(c)

    if msu:
        # Активные дни по сервисам (bar)
        c = ch(s, "Активные дни по сервисам", "echarts_timeseries_bar", msu,
               {"x_axis": "service_name", "metrics": [{"expressionType": "SQL", "sqlExpression": "avg(active_days)", "label": "Средн. активных дней", "expressionType": "SQL"}],
                "groupby": [], "row_limit": 20, "show_legend": True})
        if c: C.append(c)

    if aa:
        # Падения использования: по severity (bar)
        c = ch(s, "Падения использования по критичности", "echarts_timeseries_bar", aa,
               {"x_axis": "severity", "metrics": [{"expressionType": "SQL", "sqlExpression": "count()", "label": "Падения", "expressionType": "SQL"}],
                "groupby": [], "row_limit": 10, "show_legend": True,
                "adhoc_filters": [{"clause": "WHERE", "expressionType": "SIMPLE", "subject": "anomaly_type", "operator": "==", "comparator": "service_usage_drop", "filterOptionName": "f2"}]})
        if c: C.append(c)

        # Падения использования по дням (line)
        c = ch(s, "Падения использования по дням", "echarts_timeseries_line", aa,
               {"x_axis": "detected_at", "time_grain_sqla": "P1D",
                "metrics": [{"expressionType": "SQL", "sqlExpression": "count()", "label": "Падения", "expressionType": "SQL"}],
                "groupby": ["severity"], "row_limit": 1000, "show_legend": True,
                "adhoc_filters": [{"clause": "WHERE", "expressionType": "SIMPLE", "subject": "anomaly_type", "operator": "==", "comparator": "service_usage_drop", "filterOptionName": "f2b"}]})
        if c: C.append(c)

        # Распределение аномалий по типам (pie)
        c = ch(s, "Аномалии по типу", "pie", aa,
               {"groupby": ["anomaly_type"], "metric": {"expressionType": "SQL", "sqlExpression": "count()", "label": "count", "expressionType": "SQL"},
                "row_limit": 10, "show_labels": True, "show_legend": True, "sort_by_metric": True})
        if c: C.append(c)

    save_dash("Использование сервисов", "service-usage-anomalies", C)

    # ================================================================
    # ДАШБОРД 3: UX-ЗАТРУДНЕНИЯ
    # ================================================================
    C = []
    dfs = ds.get("daily_friction_stats")
    ux = ds.get("raw_ux_events")
    cfb = ds.get("client_friction_baseline")

    if dfs:
        # Friction-события по дням (area)
        c = ch(s, "Friction-события по дням", "echarts_area", dfs,
               {"x_axis": "date", "time_grain_sqla": "P1D",
                "metrics": [{"expressionType": "SQL", "sqlExpression": "sum(friction_event_count)", "label": "Всего friction", "expressionType": "SQL"},
                            {"expressionType": "SQL", "sqlExpression": "sum(rage_click_count)", "label": "Rage clicks", "expressionType": "SQL"},
                            {"expressionType": "SQL", "sqlExpression": "sum(idle_count)", "label": "Idle", "expressionType": "SQL"},
                            {"expressionType": "SQL", "sqlExpression": "sum(ui_error_count)", "label": "UI ошибки", "expressionType": "SQL"}],
                "groupby": [], "row_limit": 10000, "show_legend": True})
        if c: C.append(c)

        # Успешность воронок по дням (line)
        c = ch(s, "Успешность воронок по дням", "echarts_timeseries_line", dfs,
               {"x_axis": "date", "time_grain_sqla": "P1D",
                "metrics": [{"expressionType": "SQL", "sqlExpression": "avg(funnel_success_rate)", "label": "Успешность", "expressionType": "SQL"}],
                "groupby": [], "row_limit": 10000, "show_legend": True, "y_axis_format": ",.0%"})
        if c: C.append(c)

        # Среднее время задачи по дням (line)
        c = ch(s, "Среднее время задачи по дням", "echarts_timeseries_line", dfs,
               {"x_axis": "date", "time_grain_sqla": "P1D",
                "metrics": [{"expressionType": "SQL", "sqlExpression": "avg(avg_task_duration_sec)", "label": "Секунды", "expressionType": "SQL"}],
                "groupby": [], "row_limit": 10000, "show_legend": True})
        if c: C.append(c)

        # Типы friction-событий (bar — суммарно по всем дням)
        c = ch(s, "Типы UX-проблем", "echarts_timeseries_bar", dfs,
               {"x_axis": "date", "time_grain_sqla": "P1W",
                "metrics": [{"expressionType": "SQL", "sqlExpression": "sum(rage_click_count)", "label": "Rage clicks", "expressionType": "SQL"},
                            {"expressionType": "SQL", "sqlExpression": "sum(idle_count)", "label": "Idle", "expressionType": "SQL"},
                            {"expressionType": "SQL", "sqlExpression": "sum(ui_error_count)", "label": "UI ошибки", "expressionType": "SQL"},
                            {"expressionType": "SQL", "sqlExpression": "sum(exit_without_action_count)", "label": "Выход без действия", "expressionType": "SQL"}],
                "groupby": [], "row_limit": 10000, "show_legend": True})
        if c: C.append(c)

    if ux:
        # UX-события по экранам (bar)
        c = ch(s, "UX-события по экранам", "echarts_timeseries_bar", ux,
               {"x_axis": "screen", "metrics": [{"expressionType": "SQL", "sqlExpression": "count()", "label": "События", "expressionType": "SQL"}],
                "groupby": ["event_type"], "row_limit": 50, "show_legend": True})
        if c: C.append(c)

    if aa:
        # Аномалии friction: количество по severity (bar)
        c = ch(s, "Аномалии friction по критичности", "echarts_timeseries_bar", aa,
               {"x_axis": "severity", "metrics": [{"expressionType": "SQL", "sqlExpression": "count()", "label": "Аномалии", "expressionType": "SQL"}],
                "groupby": [], "row_limit": 10, "show_legend": True,
                "adhoc_filters": [{"clause": "WHERE", "expressionType": "SIMPLE", "subject": "anomaly_type", "operator": "==", "comparator": "ux_friction_spike", "filterOptionName": "f3"}]})
        if c: C.append(c)

        # Аномалии friction по дням (line)
        c = ch(s, "Аномалии friction по дням", "echarts_timeseries_line", aa,
               {"x_axis": "detected_at", "time_grain_sqla": "P1D",
                "metrics": [{"expressionType": "SQL", "sqlExpression": "count()", "label": "Аномалии", "expressionType": "SQL"}],
                "groupby": ["severity"], "row_limit": 1000, "show_legend": True,
                "adhoc_filters": [{"clause": "WHERE", "expressionType": "SIMPLE", "subject": "anomaly_type", "operator": "==", "comparator": "ux_friction_spike", "filterOptionName": "f3b"}]})
        if c: C.append(c)

    save_dash("UX-затруднения", "friction-turnover", C)


def main():
    if not wait_for_superset():
        sys.exit(1)
    clean_all()
    s = api_session()
    marts_db, raw_db = setup_databases(s)
    if not marts_db:
        sys.exit(1)
    ds = setup_datasets(marts_db, raw_db)
    setup_dashboards(ds)
    log.info("=== Init complete ===")


if __name__ == "__main__":
    main()
