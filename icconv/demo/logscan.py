# -*- coding: utf-8 -*-
"""日志扫描 CLI：扫描几个月的查询日志，找出受影响语句并按执行次数/多花时间排序。

用法：
  python -m icconv.demo.logscan --path ./demo_data --logs ./query.log.jsonl
"""
from __future__ import annotations

import argparse
import json
import os

from ..log_scanner import LogScanner
from ..storage import Database
from .make_logs import generate_logs


def main(argv=None):
    ap = argparse.ArgumentParser(description="查询日志隐式转换扫描")
    ap.add_argument("--path", default="./demo_data", help="模拟库目录")
    ap.add_argument("--logs", default=None, help="日志文件（不传则自动生成）")
    ap.add_argument("--gen", type=int, default=4000, help="自动生成日志条数")
    ap.add_argument("--no-simulate", action="store_true",
                    help="只做静态分析，不在模拟库实测改写前后耗时")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    db = Database(args.path)
    db.load()

    log_path = args.logs or "./demo_data/sample_query.jsonl"
    slow_path = log_path.rsplit(".", 1)[0] + ".slow.log"
    if not os.path.exists(log_path):
        n = generate_logs(log_path, slow_path, n=args.gen)
        print(f"已生成 {n} 条日志：{log_path} / {slow_path}")

    scanner = LogScanner(db)
    entries = scanner.parse_file(log_path)
    print(f"解析日志 {len(entries)} 条，开始分析（simulate={not args.no_simulate}）…")
    affected = scanner.scan(entries, simulate=not args.no_simulate)

    print("=" * 100)
    print(f"受影响指纹 {len(affected)} 个，Top {args.top}：")
    print(f"{'执行次数':>6} {'日志累计ms':>11} {'原语句ms':>9} {'改写ms':>9} "
          f"{'估算多花ms':>11}  规则 / 语句")
    for a in affected[: args.top]:
        print(f"{a.exec_count:>6} {a.total_log_ms:>11.1f} "
              f"{(a.sim_original_ms or 0):>9.3f} {(a.sim_rewritten_ms or 0):>9.3f} "
              f"{(a.est_extra_ms or 0):>11.3f}  {','.join(a.rules)}")
        print(f"        {a.sample_sql[:110]}")
        if a.equivalent_rewrite:
            print(f"        ↳ 等价改写: {a.equivalent_rewrite[:100]}")
        if a.needs_data_governance:
            print("        ↳ 无等价改写，需先做数据治理（前导零/非法字符等），附 DDL 建议")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(scanner.to_records(affected), f, ensure_ascii=False,
                      indent=1, default=str)
        print(f"\n完整结果写入 {args.out}")


if __name__ == "__main__":
    main()
