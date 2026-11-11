"""标准库 HTTP 服务：JSON API + 单页前端 + CSV 下载。

不依赖 Flask 或 cgi（Python 3.13 已移除 cgi），内置一个精简 multipart 解析器。
"""
from __future__ import annotations

import json
import re
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .config import Config
from .db import connect, init_db
from .engine import REASON_MESSAGES, recompute
from .importer import ImportError_, import_file
from .queries import (
    export_daily,
    export_order_detail,
    export_results,
    get_order,
    list_daily,
    list_exceptions,
    list_imports,
    list_results,
    results_summary,
    trace_txn,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = {"index.html", "app.js", "styles.css"}
MAX_UPLOAD = 100 * 1024 * 1024  # 100MB


class Handler(BaseHTTPRequestHandler):
    db_path: Path
    config: Config

    # ------------------------------------------------------------ helpers
    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body, content_type, status=200, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header(
                "Content-Disposition", f'attachment; filename="{filename}"'
            )
        self.end_headers()
        self.wfile.write(body)

    def _conn(self):
        conn = connect(self.db_path)
        init_db(conn)
        return conn

    def _query(self):
        return parse_qs(urlsplit(self.path).query, keep_blank_values=True)

    def _one(self, params, key):
        values = params.get(key)
        return values[0] if values else None

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            raise ValueError("文件超过 100MB 限制")
        return self.rfile.read(length)

    # ------------------------------------------------------------ routing
    def do_GET(self):  # noqa: N802
        try:
            path = urlsplit(self.path).path
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path.lstrip("/") in STATIC_FILES:
                return self._static(path.lstrip("/"))
            if path == "/api/config":
                return self._api_config()
            if path == "/api/imports":
                return self._api_imports()
            if path == "/api/results":
                return self._api_results()
            if path == "/api/daily":
                return self._api_daily()
            if path == "/api/exceptions":
                return self._api_exceptions()
            if path == "/api/trace":
                return self._api_trace()
            if path.startswith("/api/orders/"):
                return self._api_order(unquote(path[len("/api/orders/"):]))
            if path == "/api/export/results.csv":
                return self._api_export_results()
            if path == "/api/export/daily.csv":
                return self._api_export_daily()
            if path.startswith("/api/export/orders/"):
                return self._api_export_order(unquote(path[len("/api/export/orders/"):]))
            if path == "/api/health":
                return self._send_json({"ok": True})
            return self._send_json({"error": "not found"}, 404)
        except Exception as exc:  # pragma: no cover - 顶层兜底
            traceback.print_exc()
            return self._send_json({"error": str(exc)}, 500)

    def do_POST(self):  # noqa: N802
        try:
            path = urlsplit(self.path).path
            if path == "/api/import":
                return self._api_import()
            if path == "/api/recompute":
                return self._api_recompute()
            return self._send_json({"error": "not found"}, 404)
        except ImportError_ as exc:
            return self._send_json({"error": str(exc)}, 400)
        except Exception as exc:  # pragma: no cover
            traceback.print_exc()
            return self._send_json({"error": str(exc)}, 500)

    def log_message(self, fmt, *args):  # 安静一点
        pass

    # ------------------------------------------------------------ handlers
    def _static(self, name: str):
        if name == "":
            name = "index.html"
        if "/" in name or name not in STATIC_FILES:
            return self._send_json({"error": "not found"}, 404)
        path = STATIC_DIR / name
        if not path.exists():
            return self._send_json({"error": "not found"}, 404)
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
        }.get(path.suffix, "application/octet-stream")
        self._send_bytes(path.read_bytes(), content_type)

    def _api_config(self):
        return self._send_json(
            {
                "currency_scales": self.config.currency_scales,
                "default_scale": self.config.default_scale,
                "reason_messages": REASON_MESSAGES,
                "status_labels": {
                    "MATCH": "一致",
                    "MISMATCH": "有差额",
                    "EXCEPTION": "异常",
                },
            }
        )

    def _api_imports(self):
        conn = self._conn()
        try:
            return self._send_json({"imports": list_imports(conn), "errors": list_exceptions(conn)["row_errors"]})
        finally:
            conn.close()

    def _api_results(self):
        params = self._query()
        conn = self._conn()
        try:
            rows = list_results(
                conn,
                date=self._one(params, "date"),
                status=self._one(params, "status"),
                currency=self._one(params, "currency"),
                q=self._one(params, "q"),
                reason=self._one(params, "reason"),
            )
            return self._send_json({"results": rows, "summary": results_summary(rows)})
        finally:
            conn.close()

    def _api_order(self, order_id: str):
        params = self._query()
        version = self._one(params, "version")
        conn = self._conn()
        try:
            order = get_order(conn, order_id, int(version) if version else None)
            if order is None:
                return self._send_json({"error": "订单不存在"}, 404)
            return self._send_json({"order": order})
        finally:
            conn.close()

    def _api_daily(self):
        params = self._query()
        conn = self._conn()
        try:
            return self._send_json({"daily": list_daily(conn, self._one(params, "date"))})
        finally:
            conn.close()

    def _api_exceptions(self):
        conn = self._conn()
        try:
            return self._send_json(list_exceptions(conn))
        finally:
            conn.close()

    def _api_trace(self):
        params = self._query()
        txn_id = self._one(params, "txn_id")
        if not txn_id:
            return self._send_json({"error": "缺少 txn_id"}, 400)
        conn = self._conn()
        try:
            return self._send_json(trace_txn(conn, txn_id))
        finally:
            conn.close()

    def _api_export_results(self):
        params = self._query()
        conn = self._conn()
        try:
            body = export_results(
                conn,
                date=self._one(params, "date"),
                status=self._one(params, "status"),
                currency=self._one(params, "currency"),
                q=self._one(params, "q"),
                reason=self._one(params, "reason"),
            )
            return self._send_bytes(body, "text/csv; charset=utf-8", filename="recon_results.csv")
        finally:
            conn.close()

    def _api_export_daily(self):
        params = self._query()
        conn = self._conn()
        try:
            body = export_daily(conn, self._one(params, "date"))
            return self._send_bytes(body, "text/csv; charset=utf-8", filename="recon_daily.csv")
        finally:
            conn.close()

    def _api_export_order(self, order_id: str):
        params = self._query()
        version = self._one(params, "version")
        conn = self._conn()
        try:
            body = export_order_detail(conn, order_id, int(version) if version else None)
            if body is None:
                return self._send_json({"error": "订单不存在"}, 404)
            return self._send_bytes(
                body, "text/csv; charset=utf-8", filename=f"order_{order_id}.csv"
            )
        finally:
            conn.close()

    def _api_import(self):
        params = self._query()
        file_type = self._one(params, "type")
        if file_type not in ("order", "payment", "refund"):
            return self._send_json({"error": "type 必须为 order/payment/refund"}, 400)
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            return self._send_json(
                {"error": "请使用 multipart/form-data 上传（字段名 file）"}, 400
            )
        boundary_match = re.search(r'boundary="?([^";]+)"?', content_type)
        if not boundary_match:
            return self._send_json({"error": "缺少 boundary"}, 400)
        body = self._body()
        parts = parse_multipart(body, boundary_match.group(1))
        upload = parts.get("file")
        if not upload or not upload["data"]:
            return self._send_json({"error": "未找到上传文件字段 file"}, 400)
        filename = upload["filename"] or "upload.csv"
        conn = self._conn()
        try:
            result = import_file(conn, file_type, filename, upload["data"])
            report = recompute(conn, self.config, trigger=f"import:{file_type}")
            return self._send_json({"import": result, "recompute": report})
        finally:
            conn.close()

    def _api_recompute(self):
        conn = self._conn()
        try:
            report = recompute(conn, self.config)
            return self._send_json({"recompute": report})
        finally:
            conn.close()


def parse_multipart(body: bytes, boundary: str) -> dict:
    """解析 multipart/form-data，返回 {字段名: {"filename": str|None, "data": bytes}}。"""
    delimiter = b"--" + boundary.encode("utf-8")
    chunks = body.split(delimiter)
    fields = {}
    for chunk in chunks[1:]:
        if chunk.startswith(b"--"):
            break
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        if not chunk:
            continue
        if b"\r\n\r\n" not in chunk:
            continue
        raw_headers, data = chunk.split(b"\r\n\r\n", 1)
        headers = {}
        for line in raw_headers.decode("utf-8", "replace").split("\r\n"):
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        disposition = headers.get("content-disposition", "")
        name_match = re.search(r'name="([^"]*)"', disposition)
        file_match = re.search(r'filename="([^"]*)"', disposition)
        if not name_match:
            continue
        fields[name_match.group(1)] = {
            "filename": file_match.group(1) if file_match else None,
            "data": data,
        }
    return fields


Handler.db_path = Path("data/recon.db")
Handler.config = Config()


def run_server(db_path, config_path, host="127.0.0.1", port=8000):
    Handler.db_path = Path(db_path)
    Handler.config = Config.load(config_path)
    conn = connect(Handler.db_path)
    init_db(conn)
    conn.close()
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"对账页面已启动: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
