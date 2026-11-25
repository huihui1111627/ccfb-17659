# -*- coding: utf-8 -*-
"""单条查询分析器：转换分析 + 索引可用性 + 真实执行 + 风险提示。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .parser import QueryParser
from .planner import PlanNode, Planner, resolve_columns


@dataclass
class ConditionReport:
    context: str                    # where / join
    text: str
    op: str
    rule: str
    rule_text: str
    conversions: list               # 每个转换步骤
    index_blocked: bool
    index_block_reason: str | None
    chosen_index: str | None
    access: str                     # index_seek / seq_scan
    risks: list
    runtime_warnings: list


@dataclass
class QueryReport:
    sql: str
    tables: list
    conditions: list = field(default_factory=list)
    plan: dict = field(default_factory=dict)
    result_rows: int = 0
    elapsed_ms: float = 0.0
    runtime_warnings: list = field(default_factory=list)
    has_implicit_conversion: bool = False
    has_index_block: bool = False
    has_correctness_risk: bool = False   # 实际执行中观察到告警（真实发生）
    has_potential_risk: bool = False     # 静态潜在风险（数据变化后可能出问题）
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class Analyzer:
    def __init__(self, db):
        self.db = db

    def _new_planner(self):
        # 每次分析使用全新的 parser/planner，杜绝跨查询的缓存残留
        return Planner(self.db)

    def analyze(self, sql: str, execute: bool = True, repeats: int = 3) -> QueryReport:
        planner = self._new_planner()
        qp = planner.qp
        try:
            qi = qp.parse(sql)
            analyses = [planner.analyze_predicate(qi, p) for p in qi.predicates]
            report = QueryReport(sql=sql, tables=[
                {"name": t.name, "alias": t.alias,
                 "rows": self.db.row_count(t.name)} for t in qi.tables])

            if execute:
                rows, plan, _, warnings = planner.run(qi, repeats=repeats)
                report.result_rows = len(rows)
                report.plan = _node_to_dict(plan)
                report.elapsed_ms = round(plan.elapsed_ms, 4)
                report.runtime_warnings = _summarize_warnings(warnings)
                # 把运行时告警按条件归集
                self._attach_runtime_warnings(analyses, warnings)
                report._result_rows_values = rows  # 内部使用，改写对比用
            else:
                report.plan = {}

            warn_summary = _summarize_warnings
            for a in analyses:
                p = a.predicate
                if a.sargable:
                    access = "index_seek"
                elif a.index_block_reason is not None:
                    access = "seq_scan_blocked_by_coercion"
                else:
                    access = "seq_scan_no_index"
                cond = ConditionReport(
                    context=p.context,
                    text=p.node.sql(dialect="mysql"),
                    op=p.op,
                    rule=a.rule,
                    rule_text=a.rule_text,
                    conversions=a.steps,
                    index_blocked=a.index_block_reason is not None,
                    index_block_reason=a.index_block_reason,
                    chosen_index=a.chosen_index,
                    access=access,
                    risks=a.potential_risks,
                    runtime_warnings=warn_summary(a.runtime_warnings),
                )
                report.conditions.append(cond)

            report.has_implicit_conversion = any(
                c.rule != "NONE" for c in report.conditions)
            report.has_index_block = any(c.index_blocked for c in report.conditions)
            # 实际风险：执行中真的产生了转换告警（前导零丢失、截断、非法日期…）
            report.has_correctness_risk = bool(report.runtime_warnings)
            # 静态潜在风险：规则上存在隐患，即使本次数据没触发
            report.has_potential_risk = any(c.risks for c in report.conditions)
            return report
        except Exception as exc:  # 解析/执行失败也要给出结构化结果
            return QueryReport(sql=sql, tables=[], error=f"{type(exc).__name__}: {exc}")

    def _attach_runtime_warnings(self, analyses, warnings):
        # 简化归集：所有告警附给第一个发生转换的条件
        if not warnings:
            return
        for a in analyses:
            if a.steps:
                a.runtime_warnings = list(warnings)
                break

    def execute_only(self, sql: str, repeats: int = 1):
        """供改写对比：返回结果行（不做分析）。"""
        planner = self._new_planner()
        qi = planner.qp.parse(sql)
        rows, _, _, _ = planner.run(qi, repeats=repeats)
        return rows


def _summarize_warnings(warnings) -> list:
    """逐行产生的告警按 code 去重，保留计数与最多 3 个样例。"""
    grouped = {}
    for w in warnings:
        g = grouped.setdefault(w.code, {"code": w.code, "message": w.message,
                                        "count": 0, "samples": []})
        g["count"] += 1
        if len(g["samples"]) < 3 and w.detail not in g["samples"]:
            g["samples"].append(w.detail)
    return list(grouped.values())


def _node_to_dict(node: PlanNode) -> dict:
    return {
        "kind": node.kind, "table": node.table, "detail": node.detail,
        "index_name": node.index_name, "rows_examined": node.rows_examined,
        "rows_returned": node.rows_returned,
        "elapsed_ms": round(node.elapsed_ms, 4),
        "blocked_by": node.blocked_by,
        "children": [_node_to_dict(c) for c in node.children],
    }
