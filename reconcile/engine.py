"""对账引擎：计算订单净实收、生成版本化结算日结果、维护跨日退款调整。

核心口径（全程 Decimal，不做任何换汇）：
    支付总额 = 同一订单下、币种与订单一致的支付事件金额合计
    有效退款 = 关联到该订单有效支付、币种一致、且不超过剩余支付额的退款
    净实收   = 支付总额 - 有效退款
    差额     = 净实收 - 订单应收（订单应收已扣除售后应退）
    超额退款、找不到原支付的退款、币种不一致分别作为原因列出。

结算日 = 该订单最早一笔“有效支付”的北京时间日期。
退款可能跨天到达：订单结果与相关结算日汇总都会生成新版本，旧版本保留。
重复导入且数据未变化时，canonical snapshot 不变，不产生新版本。
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from .config import Config
from .money import ZERO, format_amount, quantize
from .timeutil import BEIJING_TZ

REASON_MESSAGES = {
    "NO_VALID_PAYMENT": "无有效支付流水",
    "PAYMENT_CURRENCY_MISMATCH": "支付币种与订单币种不一致（未换汇，已排除）",
    "REFUND_CURRENCY_MISMATCH": "退款币种与原支付/订单币种不一致（未换汇，已排除）",
    "EXCESS_REFUND": "退款超过支付总额（超出部分单列，不计入有效退款）",
    "CROSS_DAY_REFUND": "跨日退款（退款发生日与订单结算日不同）",
    "TXN_CONFLICT": "同号流水内容冲突（保留首次事件，后续事件未计入）",
    "ORDER_CONFLICT": "订单号重复且内容不同（保留首次导入版本）",
}
BLOCKING_REASONS = {
    "NO_VALID_PAYMENT",
    "PAYMENT_CURRENCY_MISMATCH",
    "REFUND_CURRENCY_MISMATCH",
    "EXCESS_REFUND",
    "TXN_CONFLICT",
    "ORDER_CONFLICT",
}

MONEY_LABELS = {
    "receivable": "应收",
    "gross_paid": "支付总额",
    "valid_refund": "有效退款",
    "excess_refund": "超额退款",
    "net_received": "净实收",
    "difference": "差额",
}


def _now() -> str:
    return datetime.now(BEIJING_TZ).isoformat()


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _reason(code: str, refs: list | None = None, detail: str | None = None) -> dict:
    message = REASON_MESSAGES[code]
    if detail:
        message = f"{message}：{detail}"
    return {
        "code": code,
        "message": message,
        "blocking": code in BLOCKING_REASONS,
        "refs": sorted(refs or []),
    }


def recompute(conn, config: Config, trigger: str = "manual") -> dict:
    """全量、确定性重算；只有 canonical snapshot 变化才产生新版本。"""
    orders = _load_orders(conn)
    payments = _load_payments(conn)
    refunds = _load_refunds(conn)

    standalone = _classify_standalone(orders, payments, refunds)
    refunds_by_order = _attach_refunds(orders, payments, refunds)
    conflicts_by_order = _payment_conflicts_by_order(conn)
    order_conflicts = _order_conflicts(conn)

    existing_results = {
        row["order_id"]: row
        for row in conn.execute("SELECT * FROM recon_result WHERE current=1").fetchall()
    }

    states = {}
    changed_orders = []
    new_orders = []
    for order_id, order in orders.items():
        state, details = _compute_order(
            order,
            payments.get(order_id, []),
            refunds_by_order.get(order_id, []),
            conflicts_by_order.get(order_id, []),
            order_conflicts.get(order_id, []),
            config,
        )
        state["details"] = details
        states[order_id] = state
        snapshot = _order_snapshot(state)

        prev = existing_results.get(order_id)
        if prev is not None and prev["canonical_snapshot"] == snapshot:
            continue  # 重复导入/重算不改变已核对结果
        prev_state = json.loads(prev["canonical_snapshot"]) if prev is not None else None
        change_note = _order_change_note(prev_state, state)
        if prev is None:
            version = 1
            new_orders.append(order_id)
        else:
            version = prev["version"] + 1
            changed_orders.append(order_id)
            conn.execute(
                "UPDATE recon_result SET current=0 WHERE order_id=? AND current=1",
                (order_id,),
            )
        result_id = _insert_result(conn, state, snapshot, version, change_note)
        _insert_details(conn, result_id, state, version, details)

    # 挂账异常每次重算重建（派生数据，不版本化）
    conn.execute("DELETE FROM standalone_exception")
    _insert_standalone(conn, standalone)

    daily_changed, daily_new = _rebuild_daily(conn, states, config)

    conn.commit()
    return {
        "trigger": trigger,
        "orders_total": len(orders),
        "orders_new": len(new_orders),
        "orders_versioned": len(changed_orders),
        "orders_unchanged": len(orders) - len(new_orders) - len(changed_orders),
        "new_orders": sorted(new_orders),
        "changed_orders": sorted(changed_orders),
        "daily_new": daily_new,
        "daily_versioned": daily_changed,
        "standalone_exceptions": len(standalone),
        "payment_conflicts": sum(len(v) for v in conflicts_by_order.values()),
        "order_conflicts": sum(len(v) for v in order_conflicts.values()),
        "finished_at": _now(),
    }


# ---------------------------------------------------------------- 数据加载

def _load_orders(conn) -> dict:
    rows = conn.execute(
        "SELECT order_id, currency, receivable FROM raw_order ORDER BY order_id"
    ).fetchall()
    return {
        r["order_id"]: {
            "order_id": r["order_id"],
            "currency": r["currency"].upper(),
            "receivable": Decimal(r["receivable"]),
        }
        for r in rows
    }


def _load_payments(conn) -> dict:
    rows = conn.execute(
        "SELECT txn_id, order_id, amount, currency, event_time, source_line "
        "FROM payment_event ORDER BY event_time, txn_id"
    ).fetchall()
    by_order = defaultdict(list)
    for r in rows:
        by_order[r["order_id"]].append(
            {
                "txn_id": r["txn_id"],
                "order_id": r["order_id"],
                "amount": Decimal(r["amount"]),
                "currency": r["currency"].upper(),
                "event_time": r["event_time"],
                "date": r["event_time"][:10],
                "source_line": r["source_line"],
            }
        )
    return by_order


def _load_refunds(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT id, origin_txn_id, amount, currency, event_time, source_line "
        "FROM refund_event ORDER BY event_time, id"
    ).fetchall()
    return [
        {
            "id": r["id"],
            "origin_txn_id": r["origin_txn_id"],
            "amount": Decimal(r["amount"]),
            "currency": r["currency"].upper(),
            "event_time": r["event_time"],
            "date": r["event_time"][:10],
            "source_line": r["source_line"],
        }
        for r in rows
    ]


# ------------------------------------------------------- 孤儿与关联

def _classify_standalone(orders, payments_by_order, refunds) -> list[dict]:
    """标记找不到归属的支付/退款，作为挂账异常。"""
    payment_by_txn = {}
    for rows in payments_by_order.values():
        for p in rows:
            payment_by_txn[p["txn_id"]] = p

    items = []
    for p in payment_by_txn.values():
        if p["order_id"] not in orders:
            items.append(
                {
                    "exception_type": "ORPHAN_PAYMENT",
                    "ref_type": "payment",
                    "ref_id": p["txn_id"],
                    "order_id": p["order_id"],
                    "amount": str(p["amount"]),
                    "currency": p["currency"],
                    "event_time": p["event_time"],
                    "reason": "支付流水对应的订单不在订单文件中",
                    "source_line": p["source_line"],
                }
            )
    for r in refunds:
        origin = payment_by_txn.get(r["origin_txn_id"])
        if origin is None:
            items.append(
                {
                    "exception_type": "ORPHAN_REFUND",
                    "ref_type": "refund",
                    "ref_id": str(r["id"]),
                    "order_id": None,
                    "amount": str(r["amount"]),
                    "currency": r["currency"],
                    "event_time": r["event_time"],
                    "reason": f"找不到原支付流水 {r['origin_txn_id']}",
                    "source_line": r["source_line"],
                }
            )
        elif origin["order_id"] not in orders:
            items.append(
                {
                    "exception_type": "ORPHAN_REFUND",
                    "ref_type": "refund",
                    "ref_id": str(r["id"]),
                    "order_id": origin["order_id"],
                    "amount": str(r["amount"]),
                    "currency": r["currency"],
                    "event_time": r["event_time"],
                    "reason": f"原支付 {r['origin_txn_id']} 所属订单不在订单文件中",
                    "source_line": r["source_line"],
                }
            )
    return items


def _attach_refunds(orders, payments_by_order, refunds) -> dict:
    """把退款挂到原支付所属订单；找不到原支付的退款不进入订单计算。"""
    payment_by_txn = {}
    for rows in payments_by_order.values():
        for p in rows:
            payment_by_txn[p["txn_id"]] = p
    by_order = defaultdict(list)
    for r in refunds:
        origin = payment_by_txn.get(r["origin_txn_id"])
        if origin is None or origin["order_id"] not in orders:
            continue
        by_order[origin["order_id"]].append(r)
    return by_order


def _payment_conflicts_by_order(conn) -> dict:
    by_order = defaultdict(list)
    rows = conn.execute("SELECT * FROM event_conflict WHERE event_type='payment'").fetchall()
    for r in rows:
        orders = set()
        for payload in (r["canonical"] or "", r["duplicate"] or ""):
            first = payload.split("|", 1)[0]
            if first:
                orders.add(first)
        for order_id in orders:
            by_order[order_id].append(
                {
                    "txn_id": r["txn_id"],
                    "batch_id": r["batch_id"],
                    "reason": r["reason"],
                }
            )
    return {k: sorted(v, key=lambda x: (x["txn_id"], x["batch_id"])) for k, v in by_order.items()}


def _order_conflicts(conn) -> dict:
    by_order = defaultdict(list)
    for r in conn.execute("SELECT order_id, batch_id, reason FROM order_conflict").fetchall():
        by_order[r["order_id"]].append({"batch_id": r["batch_id"], "reason": r["reason"]})
    return dict(by_order)


# ---------------------------------------------------------------- 订单计算

def _compute_order(order, payments, refunds, txn_conflicts, order_conflicts, config) -> tuple[dict, list[dict]]:
    order_id = order["order_id"]
    currency = order["currency"]
    scale = config.scale(currency)
    receivable = quantize(order["receivable"], scale)

    reasons = []
    details = []

    if order_conflicts:
        reasons.append(
            _reason(
                "ORDER_CONFLICT",
                refs=[],
                detail=f"{len(order_conflicts)} 条重复订单记录未覆盖首次版本",
            )
        )
    if txn_conflicts:
        reasons.append(
            _reason(
                "TXN_CONFLICT",
                refs=[f"txn:{c['txn_id']}" for c in txn_conflicts],
                detail="、".join(sorted({c["txn_id"] for c in txn_conflicts})),
            )
        )

    valid_payments = [p for p in payments if p["currency"] == currency]
    invalid_payments = [p for p in payments if p["currency"] != currency]

    gross = ZERO
    for p in valid_payments:
        amount = quantize(p["amount"], scale)
        gross += amount
        details.append(
            {
                "line_type": "payment",
                "event_ref": f"payment:{p['txn_id']}",
                "txn_id": p["txn_id"],
                "ref_txn_id": None,
                "amount": format_amount(amount, scale),
                "currency": p["currency"],
                "event_time": p["event_time"],
                "settlement_date": p["date"],
                "included": 1,
                "note": "有效支付",
                "source_line": p["source_line"],
            }
        )
    for p in invalid_payments:
        p_scale = config.scale(p["currency"])
        amount = quantize(p["amount"], p_scale)
        details.append(
            {
                "line_type": "payment",
                "event_ref": f"payment:{p['txn_id']}",
                "txn_id": p["txn_id"],
                "ref_txn_id": None,
                "amount": format_amount(amount, p_scale),
                "currency": p["currency"],
                "event_time": p["event_time"],
                "settlement_date": p["date"],
                "included": 0,
                "note": f"币种 {p['currency']} 与订单币种 {currency} 不一致，未计入（未换汇）",
                "source_line": p["source_line"],
            }
        )
    gross = quantize(gross, scale)

    if not valid_payments:
        if payments:
            detail = "所有支付流水币种均与订单不一致" if invalid_payments else "无支付流水"
        else:
            detail = "未找到该订单的支付流水"
        reasons.append(_reason("NO_VALID_PAYMENT", detail=detail))
    if invalid_payments:
        reasons.append(
            _reason(
                "PAYMENT_CURRENCY_MISMATCH",
                refs=[f"txn:{p['txn_id']}" for p in invalid_payments],
                detail="、".join(sorted({p["currency"] for p in invalid_payments})),
            )
        )

    # 结算日：优先最早有效支付，否则取最早支付（异常订单仍可定位日期）
    if valid_payments:
        settlement_date = min(p["date"] for p in valid_payments)
    elif payments:
        settlement_date = min(p["date"] for p in payments)
    else:
        settlement_date = None

    valid_refund_total = ZERO
    excess_total = ZERO
    remaining = gross
    for r in refunds:
        r_scale = config.scale(r["currency"])
        amount = quantize(r["amount"], r_scale)
        origin_payment = next(
            (p for p in payments if p["txn_id"] == r["origin_txn_id"]), None
        )
        currency_ok = (
            r["currency"] == currency
            and origin_payment is not None
            and origin_payment["currency"] == currency
        )
        if not currency_ok:
            reasons.append(
                _reason(
                    "REFUND_CURRENCY_MISMATCH",
                    refs=[f"refund:{r['id']}"],
                    detail=f"{r['currency']} 退款，订单币种 {currency}",
                )
            )
            details.append(
                {
                    "line_type": "refund",
                    "event_ref": f"refund:{r['id']}",
                    "txn_id": r["origin_txn_id"],
                    "ref_txn_id": r["origin_txn_id"],
                    "amount": format_amount(amount, r_scale),
                    "currency": r["currency"],
                    "event_time": r["event_time"],
                    "settlement_date": r["date"],
                    "included": 0,
                    "note": "币种不一致，未计入（未换汇）",
                    "source_line": r["source_line"],
                }
            )
            continue

        valid_part = min(amount, max(remaining, ZERO))
        excess_part = quantize(amount - valid_part, scale)
        remaining = quantize(remaining - valid_part, scale)
        valid_refund_total += valid_part

        if excess_part > ZERO:
            excess_total += excess_part
            reasons.append(
                _reason(
                    "EXCESS_REFUND",
                    refs=[f"refund:{r['id']}"],
                    detail=f"超出 {format_amount(excess_part, scale)} {currency}",
                )
            )

        cross_day = settlement_date is not None and r["date"] != settlement_date
        if cross_day:
            reasons.append(
                _reason(
                    "CROSS_DAY_REFUND",
                    refs=[f"refund:{r['id']}"],
                    detail=f"退款日 {r['date']} / 结算日 {settlement_date}",
                )
            )

        if valid_part > ZERO:
            details.append(
                {
                    "line_type": "refund",
                    "event_ref": f"refund:{r['id']}" if excess_part == ZERO else f"refund:{r['id']}:valid",
                    "txn_id": r["origin_txn_id"],
                    "ref_txn_id": r["origin_txn_id"],
                    "amount": format_amount(valid_part, scale),
                    "currency": currency,
                    "event_time": r["event_time"],
                    "settlement_date": r["date"],
                    "included": 1,
                    "note": "有效退款（跨日到账）" if cross_day else "有效退款",
                    "source_line": r["source_line"],
                }
            )
        if excess_part > ZERO:
            details.append(
                {
                    "line_type": "refund",
                    "event_ref": f"refund:{r['id']}:excess",
                    "txn_id": r["origin_txn_id"],
                    "ref_txn_id": r["origin_txn_id"],
                    "amount": format_amount(excess_part, scale),
                    "currency": currency,
                    "event_time": r["event_time"],
                    "settlement_date": r["date"],
                    "included": 0,
                    "note": "超额退款部分，未计入有效退款",
                    "source_line": r["source_line"],
                }
            )

    valid_refund_total = quantize(valid_refund_total, scale)
    excess_total = quantize(excess_total, scale)
    net = quantize(gross - valid_refund_total, scale)
    difference = quantize(net - receivable, scale)

    if any(r["blocking"] for r in reasons):
        status = "EXCEPTION"
    elif difference != ZERO:
        status = "MISMATCH"
    else:
        status = "MATCH"

    state = {
        "order_id": order_id,
        "currency": currency,
        "scale": scale,
        "receivable": format_amount(receivable, scale),
        "gross_paid": format_amount(gross, scale),
        "valid_refund": format_amount(valid_refund_total, scale),
        "excess_refund": format_amount(excess_total, scale),
        "net_received": format_amount(net, scale),
        "difference": format_amount(difference, scale),
        "status": status,
        "reasons": sorted(
            reasons, key=lambda x: (x["code"], ",".join(str(r) for r in x["refs"]))
        ),
        "settlement_date": settlement_date,
    }

    for ref, label in (
        ("summary:gross", "支付总额 = 有效支付合计"),
        ("summary:refund", "有效退款合计（从支付总额中扣减）"),
        ("summary:net", "净实收 = 支付总额 - 有效退款"),
        ("summary:difference", "差额 = 净实收 - 应收"),
    ):
        amount_value = {
            "summary:gross": gross,
            "summary:refund": valid_refund_total,
            "summary:net": net,
            "summary:difference": difference,
        }[ref]
        details.append(
            {
                "line_type": "summary",
                "event_ref": ref,
                "txn_id": None,
                "ref_txn_id": None,
                "amount": format_amount(amount_value, scale),
                "currency": currency,
                "event_time": None,
                "settlement_date": settlement_date,
                "included": 1,
                "note": label,
                "source_line": None,
            }
        )
    return state, details


# ---------------------------------------------------------------- 版本写入

def _order_snapshot(state: dict) -> str:
    payload = {k: v for k, v in state.items() if k != "scale"}
    payload["details"] = sorted(state["details"], key=lambda d: d["event_ref"])
    return _json(payload)


def _order_change_note(prev: dict | None, state: dict) -> str:
    if prev is None:
        return f"首次生成对账结果；结算日 {state['settlement_date'] or '未结算'}"

    parts = []
    if prev.get("status") != state["status"]:
        parts.append(f"状态 {prev.get('status')} → {state['status']}")
    if prev.get("settlement_date") != state["settlement_date"]:
        old = prev.get("settlement_date") or "未结算"
        new = state["settlement_date"] or "未结算"
        parts.append(f"结算日 {old} → {new}")
    for field, label in MONEY_LABELS.items():
        old, new = prev.get(field), state[field]
        if old != new:
            parts.append(f"{label} {old} → {new}")

    old_details = {d["event_ref"]: d for d in prev.get("details", []) if d["line_type"] != "summary"}
    new_details = {d["event_ref"]: d for d in state["details"] if d["line_type"] != "summary"}
    added = sorted(set(new_details) - set(old_details))
    removed = sorted(set(old_details) - set(new_details))
    if added:
        parts.append("新增明细 " + "、".join(_detail_label(new_details[r]) for r in added))
    if removed:
        parts.append("移除明细 " + "、".join(_detail_label(old_details[r]) for r in removed))

    old_reasons = {r["code"] for r in prev.get("reasons", [])}
    new_reasons = {r["code"] for r in state["reasons"]}
    for code in sorted(new_reasons - old_reasons):
        parts.append(f"新增原因 {REASON_MESSAGES[code]}")
    for code in sorted(old_reasons - new_reasons):
        parts.append(f"消除原因 {REASON_MESSAGES[code]}")

    return "；".join(parts) if parts else "重算后结果无变化"


def _detail_label(detail: dict) -> str:
    return f"{detail['event_ref']}（{detail['amount']} {detail['currency']}）"


def _insert_result(conn, state, snapshot, version, change_note) -> int:
    cur = conn.execute(
        "INSERT INTO recon_result(order_id, version, current, currency, receivable, "
        "gross_paid, valid_refund, excess_refund, net_received, difference, status, "
        "reasons, settlement_date, canonical_snapshot, change_note, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            state["order_id"],
            version,
            1,
            state["currency"],
            state["receivable"],
            state["gross_paid"],
            state["valid_refund"],
            state["excess_refund"],
            state["net_received"],
            state["difference"],
            state["status"],
            _json(state["reasons"]),
            state["settlement_date"],
            snapshot,
            change_note,
            _now(),
        ),
    )
    return cur.lastrowid


def _insert_details(conn, result_id: int, state: dict, version: int, details: list[dict]) -> None:
    for d in details:
        conn.execute(
            "INSERT INTO recon_detail(result_id, order_id, version, line_type, event_ref, "
            "txn_id, ref_txn_id, amount, currency, event_time, settlement_date, included, "
            "note, source_line) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                result_id,
                state["order_id"],
                version,
                d["line_type"],
                d["event_ref"],
                d["txn_id"],
                d["ref_txn_id"],
                d["amount"],
                d["currency"],
                d["event_time"],
                d["settlement_date"],
                d["included"],
                d["note"],
                d["source_line"],
            ),
        )


def _insert_standalone(conn, items: list[dict]) -> None:
    for item in items:
        conn.execute(
            "INSERT INTO standalone_exception(exception_type, ref_type, ref_id, order_id, "
            "amount, currency, event_time, reason, source_line, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                item["exception_type"],
                item["ref_type"],
                item["ref_id"],
                item["order_id"],
                item["amount"],
                item["currency"],
                item["event_time"],
                item["reason"],
                item["source_line"],
                _now(),
            ),
        )


# ---------------------------------------------------------------- 结算日汇总

def _rebuild_daily(conn, states, config) -> tuple[int, int]:
    """按 (结算日, 币种) 汇总当前订单结果，并记录跨日退款调整。

    net_total 为该结算日订单的最新净实收（退款到账后会重述）；
    adjustment_total 为“退款发生日 != 订单结算日”的有效退款备忘金额（负值），
    单独展示、不并入 net_total，避免与订单重述重复计算。
    """
    daily = {}
    for order_id, state in states.items():
        date = state["settlement_date"]
        if date is None:
            continue
        key = (date, state["currency"])
        entry = daily.setdefault(
            key,
            {
                "receivable_total": ZERO,
                "net_total": ZERO,
                "order_count": 0,
                "match_count": 0,
                "mismatch_count": 0,
                "exception_count": 0,
                "adjustment_total": ZERO,
                "adjustments": [],
            },
        )
        scale = config.scale(state["currency"])
        entry["receivable_total"] += Decimal(state["receivable"])
        entry["net_total"] += Decimal(state["net_received"])
        entry["order_count"] += 1
        if state["status"] == "MATCH":
            entry["match_count"] += 1
        elif state["status"] == "MISMATCH":
            entry["mismatch_count"] += 1
        else:
            entry["exception_count"] += 1
        # 跨日退款调整：引用当前版本明细中 included=1 的退款行
        for d in state["details"]:
            if (
                d["line_type"] == "refund"
                and d["included"] == 1
                and d["settlement_date"]
                and d["settlement_date"] != date
            ):
                refund_date = d["settlement_date"]
                rkey = (refund_date, state["currency"])
                rentry = daily.setdefault(
                    rkey,
                    {
                        "receivable_total": ZERO,
                        "net_total": ZERO,
                        "order_count": 0,
                        "match_count": 0,
                        "mismatch_count": 0,
                        "exception_count": 0,
                        "adjustment_total": ZERO,
                        "adjustments": [],
                    },
                )
                amount = Decimal(d["amount"])
                rentry["adjustment_total"] -= amount
                rentry["adjustments"].append(
                    {
                        "event_ref": d["event_ref"],
                        "order_id": order_id,
                        "amount": format_amount(-amount, scale),
                    }
                )

    existing = {
        (row["settlement_date"], row["currency"]): row
        for row in conn.execute(
            "SELECT * FROM daily_settlement WHERE current=1"
        ).fetchall()
    }

    changed = 0
    created = 0
    for (date, currency), entry in sorted(daily.items()):
        scale = config.scale(currency)
        entry["receivable_total"] = quantize(entry["receivable_total"], scale)
        entry["net_total"] = quantize(entry["net_total"], scale)
        entry["adjustment_total"] = quantize(entry["adjustment_total"], scale)
        entry["adjustments"].sort(key=lambda a: (a["event_ref"], a["order_id"]))
        snapshot_payload = {
            "settlement_date": date,
            "currency": currency,
            "order_count": entry["order_count"],
            "match_count": entry["match_count"],
            "mismatch_count": entry["mismatch_count"],
            "exception_count": entry["exception_count"],
            "receivable_total": format_amount(entry["receivable_total"], scale),
            "net_total": format_amount(entry["net_total"], scale),
            "adjustment_total": format_amount(entry["adjustment_total"], scale),
            "adjustments": entry["adjustments"],
        }
        snapshot = _json(snapshot_payload)
        prev = existing.get((date, currency))
        if prev is not None and prev["canonical_snapshot"] == snapshot:
            continue
        prev_state = json.loads(prev["canonical_snapshot"]) if prev is not None else None
        note = _daily_change_note(prev_state, snapshot_payload)
        if prev is None:
            version = 1
            created += 1
        else:
            version = prev["version"] + 1
            changed += 1
            conn.execute(
                "UPDATE daily_settlement SET current=0 "
                "WHERE settlement_date=? AND currency=? AND current=1",
                (date, currency),
            )
        conn.execute(
            "INSERT INTO daily_settlement(settlement_date, version, current, currency, "
            "order_count, match_count, mismatch_count, exception_count, receivable_total, "
            "net_total, adjustment_total, canonical_snapshot, change_note, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                date,
                version,
                1,
                currency,
                entry["order_count"],
                entry["match_count"],
                entry["mismatch_count"],
                entry["exception_count"],
                format_amount(entry["receivable_total"], scale),
                format_amount(entry["net_total"], scale),
                format_amount(entry["adjustment_total"], scale),
                snapshot,
                note,
                _now(),
            ),
        )
    return changed, created


def _daily_change_note(prev: dict | None, new: dict) -> str:
    if prev is None:
        if new["order_count"] == 0:
            return "首次生成；该日仅有跨日退款调整"
        return f"首次生成结算日汇总；订单 {new['order_count']} 笔"
    labels = {
        "order_count": "订单数",
        "match_count": "一致订单",
        "mismatch_count": "差异订单",
        "exception_count": "异常订单",
        "receivable_total": "应收合计",
        "net_total": "净实收合计",
        "adjustment_total": "跨日退款调整",
    }
    parts = [
        f"{label} {prev.get(field)} → {new[field]}"
        for field, label in labels.items()
        if prev.get(field) != new[field]
    ]
    old_refs = {a["event_ref"] for a in prev.get("adjustments", [])}
    new_refs = {a["event_ref"] for a in new["adjustments"]}
    added = [a for a in new["adjustments"] if a["event_ref"] not in old_refs]
    removed = [a for a in prev.get("adjustments", []) if a["event_ref"] not in new_refs]
    if added:
        parts.append("新增跨日调整 " + "、".join(f"{a['event_ref']}（{a['amount']}）" for a in added))
    if removed:
        parts.append("移除跨日调整 " + "、".join(f"{a['event_ref']}（{a['amount']}）" for a in removed))
    return "；".join(parts) if parts else "重算后结果无变化"
