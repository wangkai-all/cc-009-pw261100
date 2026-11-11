"""查询与 CSV 导出：供 HTTP 服务与 CLI 共用。"""
from __future__ import annotations

import csv
import io
import json

STATUS_LABELS = {"MATCH": "一致", "MISMATCH": "有差额", "EXCEPTION": "异常"}


def _reasons(raw: str) -> list[dict]:
    try:
        return json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []


def list_results(conn, date=None, status=None, currency=None, q=None, reason=None) -> list[dict]:
    sql = "SELECT * FROM recon_result WHERE current=1"
    args = []
    if date:
        sql += " AND settlement_date=?"
        args.append(date)
    if status:
        sql += " AND status=?"
        args.append(status)
    if currency:
        sql += " AND currency=?"
        args.append(currency.upper())
    if q:
        sql += " AND order_id LIKE ?"
        args.append(f"%{q}%")
    sql += " ORDER BY settlement_date DESC, order_id ASC"
    rows = []
    for r in conn.execute(sql, args).fetchall():
        item = dict(r)
        item["reasons"] = _reasons(item.pop("reasons"))
        item["canonical_snapshot"] = None  # 不向前端暴露内部快照
        item["status_label"] = STATUS_LABELS.get(item["status"], item["status"])
        if reason and reason not in {x["code"] for x in item["reasons"]}:
            continue
        rows.append(item)
    return rows


def results_summary(rows: list[dict]) -> dict:
    by_currency = {}
    for r in rows:
        c = by_currency.setdefault(
            r["currency"],
            {
                "currency": r["currency"],
                "order_count": 0,
                "match_count": 0,
                "mismatch_count": 0,
                "exception_count": 0,
                "receivable_total": "0",
                "net_total": "0",
                "difference_total": "0",
            },
        )
        c["order_count"] += 1
        c["match_count"] += r["status"] == "MATCH"
        c["mismatch_count"] += r["status"] == "MISMATCH"
        c["exception_count"] += r["status"] == "EXCEPTION"
        # 展示用十进制相加（Decimal 在下面导入）
        c["receivable_total"] = str(_add(c["receivable_total"], r["receivable"]))
        c["net_total"] = str(_add(c["net_total"], r["net_received"]))
        c["difference_total"] = str(_add(c["difference_total"], r["difference"]))
    return {
        "order_count": len(rows),
        "match_count": sum(1 for r in rows if r["status"] == "MATCH"),
        "mismatch_count": sum(1 for r in rows if r["status"] == "MISMATCH"),
        "exception_count": sum(1 for r in rows if r["status"] == "EXCEPTION"),
        "by_currency": sorted(by_currency.values(), key=lambda x: x["currency"]),
    }


def _add(a: str, b: str):
    from decimal import Decimal

    return Decimal(a) + Decimal(b)


def get_order(conn, order_id: str, version: int | None = None) -> dict | None:
    if version is not None:
        row = conn.execute(
            "SELECT * FROM recon_result WHERE order_id=? AND version=?",
            (order_id, version),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM recon_result WHERE order_id=? AND current=1", (order_id,)
        ).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["reasons"] = _reasons(item.pop("reasons"))
    item.pop("canonical_snapshot", None)
    item["status_label"] = STATUS_LABELS.get(item["status"], item["status"])
    item["details"] = [
        dict(d)
        for d in conn.execute(
            "SELECT * FROM recon_detail WHERE result_id=? ORDER BY id", (row["id"],)
        ).fetchall()
    ]
    item["versions"] = [
        {
            "version": v["version"],
            "current": bool(v["current"]),
            "status": v["status"],
            "status_label": STATUS_LABELS.get(v["status"], v["status"]),
            "net_received": v["net_received"],
            "difference": v["difference"],
            "change_note": v["change_note"],
            "created_at": v["created_at"],
            "settlement_date": v["settlement_date"],
        }
        for v in conn.execute(
            "SELECT * FROM recon_result WHERE order_id=? ORDER BY version DESC",
            (order_id,),
        ).fetchall()
    ]
    return item


def list_daily(conn, date=None) -> list[dict]:
    sql = "SELECT * FROM daily_settlement"
    args = []
    if date:
        sql += " WHERE settlement_date=?"
        args.append(date)
    sql += " ORDER BY settlement_date DESC, currency ASC, version DESC"
    grouped = {}
    for r in conn.execute(sql, args).fetchall():
        key = (r["settlement_date"], r["currency"])
        item = dict(r)
        try:
            snapshot = json.loads(item.pop("canonical_snapshot"))
        except json.JSONDecodeError:
            snapshot = {}
        item["adjustments"] = snapshot.get("adjustments", [])
        grouped.setdefault(key, item if r["current"] else None)
        if r["current"]:
            grouped[key] = item
    rows = []
    for (d, c), current in sorted(grouped.items(), reverse=True):
        if current is None:
            continue
        versions = [
            {
                "version": v["version"],
                "current": bool(v["current"]),
                "change_note": v["change_note"],
                "created_at": v["created_at"],
                "net_total": v["net_total"],
                "adjustment_total": v["adjustment_total"],
                "order_count": v["order_count"],
            }
            for v in conn.execute(
                "SELECT * FROM daily_settlement WHERE settlement_date=? AND currency=? "
                "ORDER BY version DESC",
                (d, c),
            ).fetchall()
        ]
        current["versions"] = versions
        rows.append(current)
    return rows


def list_exceptions(conn) -> dict:
    standalone = [dict(r) for r in conn.execute(
        "SELECT * FROM standalone_exception ORDER BY id"
    ).fetchall()]
    event_conflicts = [dict(r) for r in conn.execute(
        "SELECT * FROM event_conflict ORDER BY id"
    ).fetchall()]
    order_conflicts = [dict(r) for r in conn.execute(
        "SELECT * FROM order_conflict ORDER BY id"
    ).fetchall()]
    row_errors = [dict(r) for r in conn.execute(
        "SELECT e.*, b.file_name, b.file_type FROM import_row_error e "
        "JOIN import_batch b ON b.id=e.batch_id ORDER BY e.id"
    ).fetchall()]
    return {
        "standalone": standalone,
        "event_conflicts": event_conflicts,
        "order_conflicts": order_conflicts,
        "row_errors": row_errors,
        "counts": {
            "standalone": len(standalone),
            "event_conflicts": len(event_conflicts),
            "order_conflicts": len(order_conflicts),
            "row_errors": len(row_errors),
        },
    }


def list_imports(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM import_batch ORDER BY id DESC"
    ).fetchall()]


def trace_txn(conn, txn_id: str) -> dict:
    payment = conn.execute(
        "SELECT * FROM payment_event WHERE txn_id=?", (txn_id,)
    ).fetchone()
    refunds = [dict(r) for r in conn.execute(
        "SELECT * FROM refund_event WHERE origin_txn_id=? ORDER BY id", (txn_id,)
    ).fetchall()]
    conflicts = [dict(r) for r in conn.execute(
        "SELECT * FROM event_conflict WHERE txn_id=?", (txn_id,)
    ).fetchall()]
    details = [dict(r) for r in conn.execute(
        "SELECT d.*, r.status, r.version FROM recon_detail d "
        "JOIN recon_result r ON r.id=d.result_id "
        "WHERE (d.txn_id=? OR d.ref_txn_id=?) AND r.current=1 ORDER BY d.id",
        (txn_id, txn_id),
    ).fetchall()]
    return {
        "txn_id": txn_id,
        "payment": dict(payment) if payment else None,
        "refunds": refunds,
        "conflicts": conflicts,
        "details": details,
    }


# ---------------------------------------------------------------- CSV 导出

RESULT_COLUMNS = [
    ("settlement_date", "结算日"),
    ("order_id", "订单号"),
    ("status_label", "对账状态"),
    ("currency", "币种"),
    ("receivable", "应收金额"),
    ("gross_paid", "支付总额"),
    ("valid_refund", "有效退款"),
    ("excess_refund", "超额退款"),
    ("net_received", "净实收"),
    ("difference", "差额"),
    ("reason_text", "异常原因"),
    ("version", "结果版本"),
    ("change_note", "版本修订说明"),
]

DETAIL_COLUMNS = [
    ("version", "结果版本"),
    ("line_type", "明细类型"),
    ("event_ref", "原始记录引用"),
    ("txn_id", "流水号/原支付流水号"),
    ("amount", "金额"),
    ("currency", "币种"),
    ("event_time", "事件时间（北京时间）"),
    ("settlement_date", "事件日期"),
    ("included", "是否计入"),
    ("note", "说明"),
    ("source_line", "原始行"),
]

DAILY_COLUMNS = [
    ("settlement_date", "结算日"),
    ("currency", "币种"),
    ("version", "版本"),
    ("order_count", "订单数"),
    ("match_count", "一致"),
    ("mismatch_count", "有差额"),
    ("exception_count", "异常"),
    ("receivable_total", "应收合计"),
    ("net_total", "净实收合计"),
    ("adjustment_total", "跨日退款调整(备忘)"),
    ("change_note", "版本修订说明"),
]


def _csv_bytes(headers: list[str], rows: list[list]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    writer.writerows(rows)
    return ("﻿" + buf.getvalue()).encode("utf-8")  # BOM 便于 Excel 打开


def export_results(conn, **filters) -> bytes:
    rows = list_results(conn, **filters)
    data = []
    for r in rows:
        item = dict(r)
        item["reason_text"] = "；".join(x["message"] for x in item["reasons"])
        data.append([item.get(key, "") for key, _ in RESULT_COLUMNS])
    return _csv_bytes([label for _, label in RESULT_COLUMNS], data)


def export_order_detail(conn, order_id: str, version: int | None = None) -> bytes | None:
    order = get_order(conn, order_id, version)
    if order is None:
        return None
    data = []
    for d in order["details"]:
        item = dict(d)
        item["included"] = "是" if item["included"] else "否"
        data.append([item.get(key, "") for key, _ in DETAIL_COLUMNS])
    return _csv_bytes([label for _, label in DETAIL_COLUMNS], data)


def export_daily(conn, date=None) -> bytes:
    rows = list_daily(conn, date)
    data = [[r.get(key, "") for key, _ in DAILY_COLUMNS] for r in rows]
    return _csv_bytes([label for _, label in DAILY_COLUMNS], data)
