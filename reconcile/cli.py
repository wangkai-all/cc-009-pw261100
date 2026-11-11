"""命令行入口。

用法：
  python -m reconcile init
  python -m reconcile import order  samples/day1/orders.csv
  python -m reconcile import payment samples/day1/payments.csv
  python -m reconcile import refund  samples/day1/refunds.csv
  python -m reconcile recompute
  python -m reconcile demo            # 导入 day1 + day2 样例并重算
  python -m reconcile serve           # 启动网页
  python -m reconcile export results -o out.csv
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from .config import Config
from .db import connect, init_db
from .engine import recompute
from .importer import import_file
from .queries import export_daily, export_order_detail, export_results

DEFAULT_DB = Path("data/recon.db")
DEFAULT_CONFIG = Path("config.json")
SAMPLE_DIR = Path(__file__).resolve().parent.parent / "samples"


def _setup(args) -> tuple:
    config = Config.load(args.config)
    conn = connect(args.db)
    init_db(conn)
    return conn, config


def cmd_init(args) -> int:
    conn, _ = _setup(args)
    conn.close()
    print(f"数据库已初始化: {args.db}")
    return 0


def cmd_import(args) -> int:
    conn, config = _setup(args)
    results = []
    for path in args.files:
        content = Path(path).read_bytes()
        result = import_file(conn, args.file_type, Path(path).name, content)
        results.append(result)
        print(f"[{args.file_type}] {path}: {result['message']}")
    if not args.no_recompute:
        report = recompute(conn, config)
        _print_report(report)
    conn.close()
    return 0


def cmd_recompute(args) -> int:
    conn, config = _setup(args)
    _print_report(recompute(conn, config))
    conn.close()
    return 0


def cmd_demo(args) -> int:
    if args.reset and Path(args.db).exists():
        Path(args.db).unlink()
        for suffix in ("-wal", "-shm"):
            p = Path(str(args.db) + suffix)
            if p.exists():
                p.unlink()
    conn, config = _setup(args)

    def import_day(day: str) -> None:
        for file_type in ("order", "payment", "refund"):
            path = SAMPLE_DIR / day / f"{file_type}s.csv"
            if not path.exists():
                continue
            result = import_file(conn, file_type, path.name, path.read_bytes())
            print(f"[{day}/{path.name}] {result['message']}")

    print("== 导入 day1 并重算 ==")
    import_day("day1")
    _print_report(recompute(conn, config))
    print("\n== 导入 day2（跨日退款）并重算，观察订单与结算日新版本 ==")
    import_day("day2")
    _print_report(recompute(conn, config))
    print("\n== 重复导入 day1 文件验证幂等（版本不应变化） ==")
    import_day("day1")
    _print_report(recompute(conn, config))
    conn.close()
    return 0


def cmd_serve(args) -> int:
    from .server import run_server

    run_server(args.db, args.config, args.host, args.port)
    return 0


def cmd_export(args) -> int:
    conn, _ = _setup(args)
    if args.kind == "results":
        content = export_results(
            conn, date=args.date, status=args.status, currency=args.currency,
            q=args.q, reason=args.reason,
        )
    elif args.kind == "daily":
        content = export_daily(conn, date=args.date)
    elif args.kind == "order":
        if not args.order_id:
            print("导出订单明细需要 --order-id", file=sys.stderr)
            return 2
        content = export_order_detail(conn, args.order_id, args.version)
        if content is None:
            print("订单不存在", file=sys.stderr)
            return 1
    else:  # pragma: no cover
        return 2
    if args.output:
        Path(args.output).write_bytes(content)
        print(f"已导出 {args.output}")
    else:
        sys.stdout.buffer.write(content)
    conn.close()
    return 0


def _print_report(report: dict) -> None:
    print(
        "重算完成：订单 {orders_total} 笔（新增 {orders_new}，修订 {orders_versioned}，"
        "未变 {orders_unchanged}）；结算日汇总新增 {daily_new}、修订 {daily_versioned}；"
        "挂账异常 {standalone_exceptions}；流水冲突 {payment_conflicts}；订单冲突 {order_conflicts}".format(
            **report
        )
    )
    if report["changed_orders"]:
        print("  修订订单: " + ", ".join(report["changed_orders"]))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reconcile", description="订单与支付流水对账工具")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="配置文件路径")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="初始化数据库")
    p_init.set_defaults(func=cmd_init)

    p_import = sub.add_parser("import", help="导入 CSV")
    p_import.add_argument("file_type", choices=["order", "payment", "refund"])
    p_import.add_argument("files", nargs="+")
    p_import.add_argument("--no-recompute", action="store_true", help="仅导入，不触发重算")
    p_import.set_defaults(func=cmd_import)

    p_re = sub.add_parser("recompute", help="全量重算")
    p_re.set_defaults(func=cmd_recompute)

    p_demo = sub.add_parser("demo", help="导入内置样例并重算")
    p_demo.add_argument("--reset", action="store_true", help="先删除已有数据库")
    p_demo.set_defaults(func=cmd_demo)

    p_serve = sub.add_parser("serve", help="启动网页")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.set_defaults(func=cmd_serve)

    p_export = sub.add_parser("export", help="导出 CSV")
    p_export.add_argument("kind", choices=["results", "daily", "order"])
    p_export.add_argument("-o", "--output")
    p_export.add_argument("--date")
    p_export.add_argument("--status", choices=["MATCH", "MISMATCH", "EXCEPTION"])
    p_export.add_argument("--currency")
    p_export.add_argument("--q")
    p_export.add_argument("--reason")
    p_export.add_argument("--order-id")
    p_export.add_argument("--version", type=int)
    p_export.set_defaults(func=cmd_export)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
