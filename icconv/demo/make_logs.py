# -*- coding: utf-8 -*-
"""生成几个月的查询日志（JSONL 与慢日志两种格式），用于日志扫描演示。"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta

from .queries import generate_queries


def generate_logs(path_jsonl: str, path_slow: str, n: int = 4000, seed: int = 11):
    import os
    os.makedirs(os.path.dirname(path_jsonl) or ".", exist_ok=True)
    rng = random.Random(seed)
    queries = generate_queries(total=n, seed=seed)
    start = datetime(2026, 3, 1)
    with open(path_jsonl, "w", encoding="utf-8") as fj, \
         open(path_slow, "w", encoding="utf-8") as fs:
        for i, item in enumerate(queries):
            ts = start + timedelta(minutes=rng.randint(0, 60 * 24 * 120))
            # 受影响语句（尤其全表扫描/连接）日志耗时显著更高
            if item["affected"]:
                exec_ms = round(rng.lognormvariate(2.6, 0.9), 2)  # 多数 5~40ms
            else:
                exec_ms = round(rng.lognormvariate(-0.5, 0.6), 2)  # 多数 <2ms
            fj.write(json.dumps({
                "ts": ts.isoformat(timespec="seconds"),
                "sql": item["sql"], "exec_ms": exec_ms,
                "template": item["name"], "gold_affected": item["affected"],
            }, ensure_ascii=False) + "\n")
            fs.write(
                f"# Time: {ts.strftime('%Y-%m-%dT%H:%M:%S')}\n"
                f"# Query_time: {exec_ms}  Lock_time: 0.0001  Rows_sent: 10\n"
                f"{item['sql']};\n\n")
    return len(queries)
