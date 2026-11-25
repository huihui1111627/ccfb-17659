# -*- coding: utf-8 -*-
"""查询日志扫描：批量分析历史 SQL，找出所有受隐式转换影响的语句并排序。

支持两种日志格式：
1. JSONL：每行 {"ts": "...", "sql": "...", "exec_ms": 12.3}
2. 简化的慢日志文本：以 ``# Query_time: x`` 行开头，紧跟 SQL 文本

排序指标：
* 执行次数（exec_count）
* 日志中累计耗时（total_log_ms）
* 模拟库实测的"原语句 vs 等价改写后"每次多花的时间，按执行次数外推（est_extra_ms）
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from .analyzer import Analyzer
from .rewriter import Rewriter


@dataclass
class LogEntry:
    sql: str
    exec_ms: float
    ts: str | None = None


@dataclass
class AffectedQuery:
    fingerprint: str
    sample_sql: str
    exec_count: int
    total_log_ms: float
    avg_log_ms: float
    rules: list
    blocked_conditions: list
    sim_original_ms: float | None = None
    sim_rewritten_ms: float | None = None
    est_extra_ms: float | None = None
    equivalent_rewrite: str | None = None
    needs_data_governance: bool = False
    detail: dict = field(default_factory=dict)


class LogScanner:
    def __init__(self, db):
        self.db = db
        self.analyzer = Analyzer(db)
        self.rewriter = Rewriter(db)

    # ------------------------------------------------------------ 日志解析
    def parse_file(self, path: str) -> list:
        entries = []
        with open(path, encoding="utf-8") as f:
            text = f.read()
        stripped = text.lstrip()
        if stripped.startswith("{"):
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                entries.append(LogEntry(d["sql"], float(d.get("exec_ms", 0)),
                                        d.get("ts")))
        else:
            entries.extend(self._parse_slow_log(text))
        return entries

    def _parse_slow_log(self, text: str) -> list:
        entries = []
        blocks = re.split(r"(?m)^# Query_time:\s*([0-9.]+)[^\n]*\n", text)
        # blocks: [前缀, time1, body1, time2, body2, ...]
        for i in range(1, len(blocks) - 1, 2):
            qtime = float(blocks[i])
            sql_lines = [ln for ln in blocks[i + 1].splitlines()
                         if ln.strip() and not ln.lstrip().startswith("#")
                         and not ln.lstrip().lower().startswith("use ")]
            sql = re.sub(r"\s+", " ", " ".join(sql_lines)).strip().rstrip(";")
            if sql:
                entries.append(LogEntry(sql, qtime))
        return entries

    # ------------------------------------------------------------ 扫描
    def scan(self, entries: list, simulate: bool = True) -> list:
        groups: dict = {}
        for e in entries:
            try:
                fp = fingerprint(e.sql)
            except Exception:
                fp = e.sql
            g = groups.setdefault(fp, {"sample": e.sql, "count": 0,
                                       "total_ms": 0.0, "entries": []})
            g["count"] += 1
            g["total_ms"] += e.exec_ms
            g["entries"].append(e)

        affected = []
        for fp, g in groups.items():
            report = self.analyzer.analyze(g["sample"], execute=simulate)
            if report.error or not report.has_implicit_conversion:
                continue
            blocked = [c for c in report.conditions if c.index_blocked]
            if not blocked and not report.has_correctness_risk:
                continue  # 有转换但无实际影响（如常量侧转换且无数据风险），不计入受影响
            aq = AffectedQuery(
                fingerprint=fp, sample_sql=g["sample"],
                exec_count=g["count"], total_log_ms=round(g["total_ms"], 3),
                avg_log_ms=round(g["total_ms"] / g["count"], 3),
                rules=sorted({c.rule for c in report.conditions}),
                blocked_conditions=[{
                    "text": c.text, "reason": c.index_block_reason,
                    "access": c.access} for c in blocked],
                detail={"correctness_risk": report.has_correctness_risk,
                        "runtime_warnings": report.runtime_warnings},
            )
            if simulate:
                self._measure_fix(g["sample"], aq)
            affected.append(aq)

        affected.sort(key=lambda x: (
            -(x.est_extra_ms or x.total_log_ms), -x.exec_count))
        return affected

    def _measure_fix(self, sql: str, aq: AffectedQuery):
        """原语句与等价改写在模拟库各跑一次，估算每次多花的时间。"""
        orig = self.analyzer.analyze(sql, repeats=5)
        aq.sim_original_ms = orig.elapsed_ms
        try:
            sug = self.rewriter.suggest_for_query(sql)
        except Exception:
            sug = {"suggestions": []}
        eq_rewrites = [s for s in sug.get("suggestions", [])
                       if s.get("equivalent") and s.get("rewritten_sql")]
        if eq_rewrites:
            best = min(eq_rewrites, key=lambda s: len(s["rewritten_sql"]))
            fixed = self.analyzer.analyze(best["rewritten_sql"], repeats=5)
            aq.sim_rewritten_ms = fixed.elapsed_ms
            aq.equivalent_rewrite = best["rewritten_sql"]
            per_call = max(0.0, orig.elapsed_ms - fixed.elapsed_ms)
            aq.est_extra_ms = round(per_call * aq.exec_count, 4)
        else:
            # 没有等价改写（数据本身有前导零等问题），按全表扫描成本外推
            aq.needs_data_governance = True
            aq.est_extra_ms = round(aq.total_log_ms, 4)

    def to_records(self, affected: list) -> list:
        return [a.__dict__ for a in affected]


def fingerprint(sql: str) -> str:
    """把字面量替换为占位符，得到参数化指纹。"""
    tree = sqlglot.parse_one(sql, read="mysql")
    tree = tree.transform(_replace_literal)
    return tree.sql(dialect="mysql")


def _replace_literal(node):
    if isinstance(node, exp.Literal):
        return exp.Placeholder()
    return node
