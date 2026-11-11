"""CSV 导入：按流水号/订单号去重，同号不同内容报冲突，重复文件整体跳过。

去重规则：
  - 支付事件按流水号：内容语义相同（金额数值、币种、订单号、北京时间一致）的
    重复事件只算一次；同号但内容不同的事件写入 event_conflict，保留首次导入版本。
  - 退款事件按整行内容哈希：完全相同的重复退款只算一次；同一原流水号下
    多笔金额/时间不同的部分退款是正常业务，各自保留。
  - 订单按订单号：重复内容跳过，内容不同写 order_conflict，保留首次版本。

金额语义比较使用 Decimal 归一化，因此 100.0 与 100.00 视为同一内容。
"""
from __future__ import annotations

import csv
import hashlib
import io
from datetime import datetime

from .money import parse_amount, semantic_key_amount
from .timeutil import beijing_iso, parse_beijing, BEIJING_TZ

ORDER_HEADER = ["order_id", "receivable", "currency"]
PAYMENT_HEADER = ["order_id", "txn_id", "amount", "currency", "event_time"]
REFUND_HEADER = ["origin_txn_id", "amount", "currency", "event_time"]


class ImportError_(Exception):
    pass


def file_sha256(content: bytes | str) -> str:
    if isinstance(content, str):
        content = content.encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def _read_csv(content: bytes) -> tuple[list[str], list[dict]]:
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ImportError_("空文件或缺少表头")
    headers = [h.strip() for h in reader.fieldnames]
    reader.fieldnames = headers
    rows = []
    for raw in reader:
        clean = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        rows.append(
            {
                "line_no": reader.line_num,
                "raw": clean,
                "source_line": ",".join(clean.get(h, "") for h in headers),
            }
        )
    return headers, rows


def _check_header(actual: list[str], expected: list[str]) -> None:
    if actual != expected:
        raise ImportError_(f"表头应为 {','.join(expected)}，实际为 {','.join(actual)}")


def _now() -> str:
    return datetime.now(BEIJING_TZ).isoformat()


def _order_semantic(data: dict) -> str:
    return "|".join(
        [
            data["order_id"],
            semantic_key_amount(parse_amount(data["receivable"])),
            data["currency"].upper(),
        ]
    )


def _payment_semantic(data: dict) -> str:
    return "|".join(
        [
            data["order_id"],
            semantic_key_amount(parse_amount(data["amount"])),
            data["currency"].upper(),
            parse_beijing(data["event_time"]).isoformat(),
        ]
    )


def _refund_semantic(data: dict) -> str:
    return "|".join(
        [
            data["origin_txn_id"],
            semantic_key_amount(parse_amount(data["amount"])),
            data["currency"].upper(),
            parse_beijing(data["event_time"]).isoformat(),
        ]
    )


def import_file(conn, file_type: str, file_name: str, content: bytes) -> dict:
    """导入单个 CSV 文件，返回统计信息。相同文件（按 sha256）整体跳过。"""
    digest = file_sha256(content)
    existing = conn.execute(
        "SELECT id FROM import_batch WHERE file_type=? AND file_sha256=?",
        (file_type, digest),
    ).fetchone()

    headers, rows = _read_csv(content)
    expected = {
        "order": ORDER_HEADER,
        "payment": PAYMENT_HEADER,
        "refund": REFUND_HEADER,
    }.get(file_type)
    if expected is None:
        raise ImportError_(f"未知文件类型: {file_type}")
    _check_header(headers, expected)

    if existing:
        return {
            "batch_id": existing["id"],
            "skipped": True,
            "row_count": len(rows),
            "inserted": 0,
            "duplicates": 0,
            "conflicts": 0,
            "errors": 0,
            "message": "文件内容与已导入批次完全相同，跳过",
        }

    cur = conn.execute(
        "INSERT INTO import_batch(import_time, file_type, file_name, file_sha256, row_count) "
        "VALUES (?,?,?,?,?)",
        (_now(), file_type, file_name, digest, len(rows)),
    )
    batch_id = cur.lastrowid

    stats = {"inserted": 0, "duplicates": 0, "conflicts": 0, "errors": 0}
    handler = {
        "order": _upsert_order,
        "payment": _upsert_payment,
        "refund": _insert_refund,
    }[file_type]
    for row in rows:
        try:
            handler(conn, batch_id, row, stats)
        except Exception as exc:  # 行级错误不中断整批导入
            stats["errors"] += 1
            conn.execute(
                "INSERT INTO import_row_error(batch_id, file_type, row_number, raw_line, error) "
                "VALUES (?,?,?,?,?)",
                (batch_id, file_type, row["line_no"], row["source_line"], str(exc)),
            )

    conn.execute(
        "UPDATE import_batch SET status=? WHERE id=?",
        ("imported_with_errors" if stats["errors"] else "imported", batch_id),
    )
    conn.commit()
    return {
        "batch_id": batch_id,
        "skipped": False,
        "row_count": len(rows),
        **stats,
        "message": f"导入 {len(rows)} 行：新增 {stats['inserted']}，"
        f"重复 {stats['duplicates']}，冲突 {stats['conflicts']}，错误 {stats['errors']}",
    }


def _upsert_order(conn, batch_id: int, row: dict, stats: dict) -> None:
    data = row["raw"]
    order_id = data["order_id"]
    if not order_id:
        raise ValueError("订单号为空")
    currency = data["currency"].upper()
    receivable = str(parse_amount(data["receivable"]))  # 原始精度，引擎再按币种量化
    semantic = _order_semantic(data)
    stored = conn.execute(
        "SELECT order_id, currency, receivable FROM raw_order WHERE order_id=?", (order_id,)
    ).fetchone()
    if stored:
        stored_semantic = _order_semantic(
            {
                "order_id": stored["order_id"],
                "receivable": stored["receivable"],
                "currency": stored["currency"],
            }
        )
        if stored_semantic == semantic:
            stats["duplicates"] += 1
            return
        conn.execute(
            "INSERT INTO order_conflict(order_id, batch_id, payload, reason, created_at) "
            "VALUES (?,?,?,?,?)",
            (order_id, batch_id, semantic, "订单号重复且内容不同，保留首次导入版本", _now()),
        )
        stats["conflicts"] += 1
        return
    conn.execute(
        "INSERT INTO raw_order(order_id, currency, receivable, source_line, first_batch_id) "
        "VALUES (?,?,?,?,?)",
        (order_id, currency, receivable, row["source_line"], batch_id),
    )
    stats["inserted"] += 1


def _upsert_payment(conn, batch_id: int, row: dict, stats: dict) -> None:
    data = row["raw"]
    txn_id = data["txn_id"]
    if not txn_id:
        raise ValueError("支付流水号为空")
    event_time = beijing_iso(data["event_time"])
    amount = str(parse_amount(data["amount"]))
    currency = data["currency"].upper()
    semantic = _payment_semantic(data)
    stored = conn.execute(
        "SELECT order_id, amount, currency, event_time FROM payment_event WHERE txn_id=?",
        (txn_id,),
    ).fetchone()
    if stored:
        stored_semantic = _payment_semantic(dict(stored))
        if stored_semantic == semantic:
            stats["duplicates"] += 1
            return
        conn.execute(
            "INSERT INTO event_conflict(txn_id, event_type, batch_id, canonical, duplicate, "
            "reason, created_at) VALUES (?,?,?,?,?,?,?)",
            (
                txn_id,
                "payment",
                batch_id,
                stored_semantic,
                semantic,
                "流水号重复且内容不同，保留首次导入事件，后续事件不计入对账",
                _now(),
            ),
        )
        stats["conflicts"] += 1
        return
    conn.execute(
        "INSERT INTO payment_event(txn_id, order_id, amount, currency, event_time, "
        "source_line, first_batch_id) VALUES (?,?,?,?,?,?,?)",
        (txn_id, data["order_id"], amount, currency, event_time, row["source_line"], batch_id),
    )
    stats["inserted"] += 1


def _insert_refund(conn, batch_id: int, row: dict, stats: dict) -> None:
    data = row["raw"]
    origin = data["origin_txn_id"]
    if not origin:
        raise ValueError("原支付流水号为空")
    event_time = beijing_iso(data["event_time"])
    amount = str(parse_amount(data["amount"]))
    currency = data["currency"].upper()
    semantic = _refund_semantic(data)
    content_hash = hashlib.sha256(semantic.encode("utf-8")).hexdigest()
    dup = conn.execute(
        "SELECT 1 FROM refund_event WHERE content_hash=? LIMIT 1", (content_hash,)
    ).fetchone()
    if dup:
        stats["duplicates"] += 1
        return
    conn.execute(
        "INSERT INTO refund_event(origin_txn_id, amount, currency, event_time, source_line, "
        "batch_id, content_hash) VALUES (?,?,?,?,?,?,?)",
        (origin, amount, currency, event_time, row["source_line"], batch_id, content_hash),
    )
    stats["inserted"] += 1
