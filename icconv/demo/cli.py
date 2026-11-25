# -*- coding: utf-8 -*-
"""一键构建演示库并运行端到端评估。

用法：
  python -m icconv.demo.cli build --path ./demo_data
  python -m icconv.demo.cli evaluate --path ./demo_data --queries 3000
  python -m icconv.demo.cli review --path ./demo_data
"""
from __future__ import annotations

import argparse
import json
import shutil

from ..evaluate import evaluate, print_report, save_report
from ..schema_review import SchemaReviewer
from ..storage import Database
from .generate import build_database


def main(argv=None):
    ap = argparse.ArgumentParser(description="隐式转换分析演示工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="构建演示业务库（25 张表 + 脏数据）")
    b.add_argument("--path", default="./demo_data")
    b.add_argument("--scale", type=float, default=1.0)

    e = sub.add_parser("evaluate", help="跑生成查询并统计漏标/误标、改写对比")
    e.add_argument("--path", default="./demo_data")
    e.add_argument("--queries", type=int, default=3000)
    e.add_argument("--rewrite-sample", type=int, default=40)
    e.add_argument("--out", default=None)

    r = sub.add_parser("review", help="输出全库列不一致清单")
    r.add_argument("--path", default="./demo_data")

    args = ap.parse_args(argv)

    if args.cmd == "build":
        shutil.rmtree(args.path, ignore_errors=True)
        db = build_database(args.path, scale=args.scale)
        total = sum(db.row_count(t) for t in db.catalog.tables)
        print(f"已构建 {len(db.catalog.tables)} 张表、共 {total} 行数据于 {args.path}")

    elif args.cmd == "evaluate":
        db = Database(args.path)
        db.load()
        res = evaluate(db, n_queries=args.queries,
                       rewrite_sample=args.rewrite_sample)
        print_report(res)
        if args.out:
            save_report(res, args.out)
            print(f"\n详细结果已写入 {args.out}")

    elif args.cmd == "review":
        db = Database(args.path)
        db.load()
        issues = SchemaReviewer(db).review()
        for i in issues:
            print(f"[{i.severity:8s}] {i.category:26s} {i.title}")
            print(f"            列: {', '.join(i.columns)}")
            print(f"            建议: {i.suggestion}")
        print(f"\n共 {len(issues)} 项")


if __name__ == "__main__":
    main()
