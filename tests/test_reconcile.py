"""对账工具测试：金额精度、去重冲突、部分退款、跨日版本与导出。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reconcile.config import Config
from reconcile.db import connect, init_db
from reconcile.engine import recompute
from reconcile.importer import import_file
from reconcile.money import parse_amount, quantize
from reconcile.queries import export_daily, export_order_detail, export_results, get_order, list_exceptions
from reconcile.server import parse_multipart as server_parse_multipart
from reconcile.timeutil import beijing_date, parse_beijing

ORDER_CSV = "order_id,receivable,currency\n{rows}\n"
PAY_CSV = "order_id,txn_id,amount,currency,event_time\n{rows}\n"
REFUND_CSV = "origin_txn_id,amount,currency,event_time\n{rows}\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "recon.db")
        self.conn = connect(self.db)
        init_db(self.conn)
        self.config = Config()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def import_orders(self, text, name="orders.csv"):
        return import_file(self.conn, "order", name, text.encode("utf-8"))

    def import_payments(self, text, name="payments.csv"):
        return import_file(self.conn, "payment", name, text.encode("utf-8"))

    def import_refunds(self, text, name="refunds.csv"):
        return import_file(self.conn, "refund", name, text.encode("utf-8"))

    def order(self, order_id, version=None):
        return get_order(self.conn, order_id, version)


class TestMoneyAndTime(unittest.TestCase):
    def test_decimal_arithmetic(self):
        total = parse_amount("0.1") + parse_amount("0.2")
        self.assertEqual(total, Decimal("0.3"))
        self.assertEqual(quantize(total, 2), Decimal("0.30"))
        self.assertNotIsInstance(total, float)

    def test_float_rejected(self):
        with self.assertRaises(TypeError):
            parse_amount(1.1)

    def test_currency_scale(self):
        config = Config({"JPY": 0, "CNY": 2})
        self.assertEqual(config.scale("JPY"), 0)
        self.assertEqual(config.scale("cny"), 2)
        self.assertEqual(config.scale("XXX"), 2)
        self.assertEqual(quantize(Decimal("123.45"), 0), Decimal("123"))
        self.assertEqual(quantize(Decimal("123.5"), 0), Decimal("124"))

    def test_beijing_time_boundary(self):
        # UTC 2026-11-08 16:30 == 北京时间 2026-11-09 00:30
        self.assertEqual(beijing_date("2026-11-08T16:30:00Z"), "2026-11-09")
        self.assertEqual(beijing_date("2026-11-09 00:30:00"), "2026-11-09")
        self.assertEqual(beijing_date("2026-11-08 23:30:00+08:00"), "2026-11-08")
        self.assertEqual(parse_beijing("2026-11-08T16:30:00Z").utcoffset().total_seconds(), 8 * 3600)


class TestImportDedup(Base):
    def test_identical_payment_event_deduped(self):
        self.import_orders(ORDER_CSV.format(rows="S1,100.00,CNY"))
        text = PAY_CSV.format(rows=(
            "S1,P1,100.00,CNY,2026-11-09 10:00:00\n"
            "S1,P1,100.0,CNY,2026-11-09T10:00:00+08:00\n"
        ))
        stats = self.import_payments(text)
        self.assertEqual(stats["inserted"], 1)
        self.assertEqual(stats["duplicates"], 1)
        self.assertEqual(stats["conflicts"], 0)
        report = recompute(self.conn, self.config)
        order = self.order("S1")
        self.assertEqual(order["gross_paid"], "100.00")
        self.assertEqual(order["status"], "MATCH")
        self.assertEqual(report["orders_new"], 1)

    def test_same_txn_different_content_conflicts(self):
        self.import_orders(ORDER_CSV.format(rows="S1,100.00,CNY"))
        text = PAY_CSV.format(rows=(
            "S1,P1,100.00,CNY,2026-11-09 10:00:00\n"
            "S1,P1,120.00,CNY,2026-11-09 10:00:00\n"
        ))
        stats = self.import_payments(text)
        self.assertEqual(stats["conflicts"], 1)
        recompute(self.conn, self.config)
        order = self.order("S1")
        self.assertEqual(order["gross_paid"], "100.00")  # 保留首次事件
        self.assertEqual(order["status"], "EXCEPTION")
        self.assertIn("TXN_CONFLICT", {r["code"] for r in order["reasons"]})
        conflicts = list_exceptions(self.conn)["event_conflicts"]
        self.assertEqual(conflicts[0]["txn_id"], "P1")
        self.assertIn("保留首次", conflicts[0]["reason"])

    def test_identical_file_skipped(self):
        self.import_orders(ORDER_CSV.format(rows="S1,100.00,CNY"))
        first = self.import_payments(PAY_CSV.format(rows="S1,P1,100.00,CNY,2026-11-09 10:00:00"))
        second = self.import_payments(PAY_CSV.format(rows="S1,P1,100.00,CNY,2026-11-09 10:00:00"))
        self.assertFalse(first["skipped"])
        self.assertTrue(second["skipped"])
        self.assertEqual(second["batch_id"], first["batch_id"])

    def test_order_conflict_keeps_first_version(self):
        self.import_orders(ORDER_CSV.format(rows=(
            "S1,90.00,CNY\nS1,95.00,CNY\n"
        )))
        stats = self.import_payments(PAY_CSV.format(rows="S1,P1,90.00,CNY,2026-11-09 10:00:00"))
        self.assertEqual(stats["inserted"], 1)
        recompute(self.conn, self.config)
        order = self.order("S1")
        self.assertEqual(order["receivable"], "90.00")
        self.assertEqual(order["status"], "EXCEPTION")
        self.assertIn("ORDER_CONFLICT", {r["code"] for r in order["reasons"]})

    def test_bad_row_does_not_abort_batch(self):
        text = PAY_CSV.format(rows=(
            "S1,P1,abc,CNY,2026-11-09 10:00:00\n"
            "S1,P2,100.00,CNY,2026-11-09 10:00:00\n"
        ))
        stats = self.import_payments(text)
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(stats["inserted"], 1)
        errors = list_exceptions(self.conn)["row_errors"]
        self.assertEqual(errors[0]["row_number"], 2)


class TestOrderCalculation(Base):
    def test_split_payments_and_partial_refunds(self):
        self.import_orders(ORDER_CSV.format(rows="S1,450.00,CNY"))
        self.import_payments(PAY_CSV.format(rows=(
            "S1,P1,600.00,CNY,2026-11-09 09:00:00\n"
        )))
        self.import_refunds(REFUND_CSV.format(rows=(
            "P1,100.00,CNY,2026-11-09 10:00:00\n"
            "P1,50.00,CNY,2026-11-09 11:00:00\n"
        )))
        recompute(self.conn, self.config)
        o = self.order("S1")
        self.assertEqual(o["gross_paid"], "600.00")
        self.assertEqual(o["valid_refund"], "150.00")
        self.assertEqual(o["net_received"], "450.00")
        self.assertEqual(o["difference"], "0.00")
        self.assertEqual(o["status"], "MATCH")
        refunds = [d for d in o["details"] if d["line_type"] == "refund"]
        self.assertEqual(len(refunds), 2)
        self.assertEqual({d["amount"] for d in refunds}, {"100.00", "50.00"})
        self.assertTrue(all(d["included"] == 1 for d in refunds))

    def test_split_payments(self):
        self.import_orders(ORDER_CSV.format(rows="S1,1000.00,CNY"))
        self.import_payments(PAY_CSV.format(rows=(
            "S1,P1,400.00,CNY,2026-11-09 09:00:00\n"
            "S1,P2,600.00,CNY,2026-11-09 11:00:00\n"
        )))
        recompute(self.conn, self.config)
        o = self.order("S1")
        self.assertEqual(o["gross_paid"], "1000.00")
        self.assertEqual(o["status"], "MATCH")
        self.assertEqual(o["settlement_date"], "2026-11-09")

    def test_excess_refund(self):
        self.import_orders(ORDER_CSV.format(rows="S1,100.00,CNY"))
        self.import_payments(PAY_CSV.format(rows="S1,P1,100.00,CNY,2026-11-09 09:00:00"))
        self.import_refunds(REFUND_CSV.format(rows="P1,150.00,CNY,2026-11-09 10:00:00"))
        recompute(self.conn, self.config)
        o = self.order("S1")
        self.assertEqual(o["valid_refund"], "100.00")
        self.assertEqual(o["excess_refund"], "50.00")
        self.assertEqual(o["net_received"], "0.00")
        self.assertEqual(o["difference"], "-100.00")
        self.assertEqual(o["status"], "EXCEPTION")
        self.assertIn("EXCESS_REFUND", {r["code"] for r in o["reasons"]})
        excess = [d for d in o["details"] if d["event_ref"].endswith(":excess")]
        self.assertEqual(excess[0]["amount"], "50.00")
        self.assertEqual(excess[0]["included"], 0)

    def test_currency_mismatch_no_conversion(self):
        self.import_orders(ORDER_CSV.format(rows="S1,80.00,CNY"))
        self.import_payments(PAY_CSV.format(rows="S1,P1,80.00,USD,2026-11-09 09:00:00"))
        recompute(self.conn, self.config)
        o = self.order("S1")
        self.assertEqual(o["gross_paid"], "0.00")
        self.assertEqual(o["difference"], "-80.00")
        self.assertEqual(o["status"], "EXCEPTION")
        codes = {r["code"] for r in o["reasons"]}
        self.assertIn("PAYMENT_CURRENCY_MISMATCH", codes)
        bad = [d for d in o["details"] if d["line_type"] == "payment"]
        self.assertEqual(bad[0]["included"], 0)
        self.assertEqual(bad[0]["currency"], "USD")

    def test_refund_currency_mismatch_is_exception_even_when_net_matches(self):
        self.import_orders(ORDER_CSV.format(rows="S1,100.00,CNY"))
        self.import_payments(PAY_CSV.format(rows="S1,P1,100.00,CNY,2026-11-09 09:00:00"))
        self.import_refunds(REFUND_CSV.format(rows="P1,10.00,USD,2026-11-10 08:00:00"))
        recompute(self.conn, self.config)
        o = self.order("S1")
        self.assertEqual(o["net_received"], "100.00")
        self.assertEqual(o["difference"], "0.00")
        self.assertEqual(o["status"], "EXCEPTION")
        self.assertIn("REFUND_CURRENCY_MISMATCH", {r["code"] for r in o["reasons"]})

    def test_orphan_refund(self):
        self.import_orders(ORDER_CSV.format(rows="S1,100.00,CNY"))
        self.import_payments(PAY_CSV.format(rows="S1,P1,100.00,CNY,2026-11-09 09:00:00"))
        self.import_refunds(REFUND_CSV.format(rows="P404,30.00,CNY,2026-11-09 10:00:00"))
        recompute(self.conn, self.config)
        items = list_exceptions(self.conn)["standalone"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["exception_type"], "ORPHAN_REFUND")
        self.assertIn("P404", items[0]["reason"])
        o = self.order("S1")
        self.assertEqual(o["status"], "MATCH")

    def test_no_valid_payment(self):
        self.import_orders(ORDER_CSV.format(rows="S1,50.00,CNY"))
        recompute(self.conn, self.config)
        o = self.order("S1")
        self.assertEqual(o["status"], "EXCEPTION")
        self.assertIn("NO_VALID_PAYMENT", {r["code"] for r in o["reasons"]})
        self.assertIsNone(o["settlement_date"])

    def test_jpy_zero_scale(self):
        self.import_orders(ORDER_CSV.format(rows="S1,1000,JPY"))
        self.import_payments(PAY_CSV.format(rows="S1,P1,1000,JPY,2026-11-09 09:00:00"))
        recompute(self.conn, self.config)
        o = self.order("S1")
        self.assertEqual(o["net_received"], "1000")
        self.assertEqual(o["status"], "MATCH")


class TestCrossDayVersioning(Base):
    def setUp(self):
        super().setUp()
        self.import_orders(ORDER_CSV.format(rows="S1,700.00,CNY"))
        self.import_payments(PAY_CSV.format(rows="S1,P1,800.00,CNY,2026-11-09 09:00:00"))
        self.report1 = recompute(self.conn, self.config)

    def test_refund_after_day_updates_order_and_dates(self):
        o1 = self.order("S1")
        self.assertEqual(o1["version"], 1)
        self.assertEqual(o1["status"], "MISMATCH")
        self.assertEqual(o1["difference"], "100.00")

        self.import_refunds(REFUND_CSV.format(rows="P1,100.00,CNY,2026-11-10 10:00:00"))
        report2 = recompute(self.conn, self.config)
        self.assertEqual(report2["orders_versioned"], 1)

        o2 = self.order("S1")
        self.assertEqual(o2["version"], 2)
        self.assertEqual(o2["status"], "MATCH")
        self.assertEqual(o2["difference"], "0.00")
        self.assertEqual(o2["net_received"], "700.00")
        self.assertIn("CROSS_DAY_REFUND", {r["code"] for r in o2["reasons"]})
        self.assertIn("差额 100.00 → 0.00", o2["change_note"])

        # 上一版保留，且不再为 current
        versions = o2["versions"]
        self.assertEqual([v["version"] for v in versions], [2, 1])
        self.assertFalse(versions[1]["current"])
        self.assertEqual(versions[1]["net_received"], "800.00")

        daily = {d["settlement_date"] + d["currency"]: d for d in self._daily()}
        day1 = daily["2026-11-09CNY"]
        self.assertEqual(day1["version"], 2)
        self.assertEqual(day1["net_total"], "700.00")
        self.assertEqual(day1["match_count"], 1)
        day2 = daily["2026-11-10CNY"]
        self.assertEqual(day2["version"], 1)
        self.assertEqual(day2["order_count"], 0)
        self.assertEqual(day2["net_total"], "0.00")
        self.assertEqual(day2["adjustment_total"], "-100.00")
        self.assertEqual(len(day2["adjustments"]), 1)
        self.assertEqual(day2["adjustments"][0]["order_id"], "S1")

    def test_recompute_is_idempotent(self):
        self.import_refunds(REFUND_CSV.format(rows="P1,100.00,CNY,2026-11-10 10:00:00"))
        recompute(self.conn, self.config)
        versions_before = self.order("S1")["versions"]
        report = recompute(self.conn, self.config)
        self.assertEqual(report["orders_versioned"], 0)
        self.assertEqual(report["orders_new"], 0)
        self.assertEqual(report["daily_versioned"], 0)
        self.assertEqual(self.order("S1")["versions"], versions_before)

    def test_reimport_after_match_does_not_change_result(self):
        self.import_refunds(REFUND_CSV.format(rows="P1,100.00,CNY,2026-11-10 10:00:00"))
        recompute(self.conn, self.config)
        o = self.order("S1")
        self.assertEqual(o["status"], "MATCH")
        self.assertEqual(o["version"], 2)
        # 再导入完全相同的退款文件：文件级跳过
        stats = self.import_refunds(REFUND_CSV.format(rows="P1,100.00,CNY,2026-11-10 10:00:00"), name="refunds-again.csv")
        self.assertTrue(stats["skipped"])
        report = recompute(self.conn, self.config)
        self.assertEqual(report["orders_versioned"], 0)
        self.assertEqual(self.order("S1")["version"], 2)

    def _daily(self):
        from reconcile.queries import list_daily

        return list_daily(self.conn)


class TestDailyAggregation(Base):
    def test_aggregates_and_versions(self):
        self.import_orders(ORDER_CSV.format(rows=(
            "S1,100.00,CNY\nS2,100.00,CNY\nS3,1000,JPY\n"
        )))
        self.import_payments(PAY_CSV.format(rows=(
            "S1,P1,100.00,CNY,2026-11-09 09:00:00\n"
            "S2,P2,50.00,CNY,2026-11-09 10:00:00\n"
            "S3,P3,1000,JPY,2026-11-09 11:00:00\n"
        )))
        recompute(self.conn, self.config)
        from reconcile.queries import list_daily
        daily = {d["settlement_date"] + d["currency"]: d for d in list_daily(self.conn)}
        cny = daily["2026-11-09CNY"]
        self.assertEqual(cny["order_count"], 2)
        self.assertEqual(cny["match_count"], 1)
        self.assertEqual(cny["mismatch_count"], 1)
        self.assertEqual(cny["exception_count"], 0)
        self.assertEqual(cny["receivable_total"], "200.00")
        self.assertEqual(cny["net_total"], "150.00")
        jpy = daily["2026-11-09JPY"]
        self.assertEqual(jpy["net_total"], "1000")


class TestExports(Base):
    def setUp(self):
        super().setUp()
        self.import_orders(ORDER_CSV.format(rows="S1,450.00,CNY"))
        self.import_payments(PAY_CSV.format(rows=(
            "S1,P1,400.00,CNY,2026-11-09 09:00:00\n"
            "S1,P2,200.00,CNY,2026-11-09 10:00:00\n"
        )))
        self.import_refunds(REFUND_CSV.format(rows="P1,150.00,CNY,2026-11-09 11:00:00"))
        recompute(self.conn, self.config)

    def test_results_csv_has_bom_and_columns(self):
        body = export_results(self.conn)
        self.assertTrue(body.startswith(b"\xef\xbb\xbf"))
        text = body.decode("utf-8-sig")
        self.assertIn("结算日,订单号,对账状态", text)
        self.assertIn("S1", text)

    def test_order_detail_csv(self):
        body = export_order_detail(self.conn, "S1")
        text = body.decode("utf-8-sig")
        self.assertIn("原始记录引用", text)
        self.assertIn("refund:", text)
        self.assertIn("450.00", text)

    def test_daily_csv(self):
        body = export_daily(self.conn)
        self.assertIn("结算日", body.decode("utf-8-sig"))
        self.assertTrue(body.startswith(b"\xef\xbb\xbf"))


class TestMultipartParser(unittest.TestCase):
    def test_parse_file_field(self):
        boundary = "testboundary"
        content = b"order_id,receivable,currency\nS1,100.00,CNY\n"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="orders.csv"\r\n'
            "Content-Type: text/csv\r\n\r\n"
        ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
        fields = server_parse_multipart(body, boundary)
        self.assertEqual(fields["file"]["filename"], "orders.csv")
        self.assertEqual(fields["file"]["data"], content)

    def test_parse_text_field(self):
        boundary = "b"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="note"\r\n\r\n'
            "hello\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        fields = server_parse_multipart(body, boundary)
        self.assertEqual(fields["note"]["data"], b"hello")


class TestHttpEndToEnd(Base):
    """线程内启动真实 HTTP 服务，验证静态页面、API、multipart 上传与导出。"""

    def setUp(self):
        super().setUp()
        import threading
        from http.server import ThreadingHTTPServer

        from reconcile.server import Handler

        Handler.db_path = self.db
        Handler.config = self.config
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def _get(self, path):
        import urllib.request

        with urllib.request.urlopen(self.base + path, timeout=10) as res:
            return res.status, res.read()

    def _upload(self, file_type, filename, content):
        import urllib.request

        boundary = "test-boundary"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: text/csv\r\n\r\n"
        ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"{self.base}/api/import?type={file_type}",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        import json as _json

        with urllib.request.urlopen(req, timeout=10) as res:
            return _json.loads(res.read())

    def test_static_files_served(self):
        for path in ("/", "/index.html", "/app.js", "/styles.css", "/static/app.js"):
            status, body = self._get(path)
            self.assertEqual(status, 200, path)
            self.assertTrue(len(body) > 100, path)

    def test_upload_and_query(self):
        payload = self._upload("order", "orders.csv", "order_id,receivable,currency\nS1,50.00,CNY\n".encode())
        self.assertEqual(payload["import"]["inserted"], 1)
        self.assertEqual(payload["recompute"]["orders_new"], 1)
        payload = self._upload(
            "payment", "payments.csv",
            b"order_id,txn_id,amount,currency,event_time\nS1,P1,50.00,CNY,2026-11-09 09:00:00\n",
        )
        self.assertEqual(payload["recompute"]["orders_versioned"], 1)

        status, body = self._get("/api/orders/S1")
        self.assertEqual(status, 200)
        order = __import__("json").loads(body)["order"]
        self.assertEqual(order["status"], "MATCH")
        self.assertEqual(order["net_received"], "50.00")

        status, body = self._get("/api/export/results.csv")
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"\xef\xbb\xbf"))
        self.assertIn("S1", body.decode("utf-8-sig"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
