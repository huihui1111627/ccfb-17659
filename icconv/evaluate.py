# -*- coding: utf-8 -*-
"""端到端评估：在几千条生成查询上统计漏标/误标，并对每条被标出语句做
原语句 vs 改写语句的计划、耗时、逐行结果对比。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from .analyzer import Analyzer
from .demo.queries import TEMPLATES, generate_queries, gold_labels
from .rewriter import Rewriter


@dataclass
class EvalResult:
    total: int = 0
    true_positive: int = 0
    false_positive: int = 0
    false_negative: int = 0
    true_negative: int = 0
    by_template: dict = field(default_factory=dict)
    rewrite_checks: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    @property
    def precision(self):
        return self.true_positive / (self.true_positive + self.false_positive) \
            if (self.true_positive + self.false_positive) else 1.0

    @property
    def recall(self):
        return self.true_positive / (self.true_positive + self.false_negative) \
            if (self.true_positive + self.false_negative) else 1.0

    @property
    def f1(self):
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


def _tool_says_affected(report) -> bool:
    return report.has_index_block or report.has_correctness_risk \
        or report.has_potential_risk


def evaluate(db, n_queries: int = 3000, seed: int = 7,
             rewrite_sample: int = 40) -> EvalResult:
    az = Analyzer(db)
    rw = Rewriter(db)
    queries = generate_queries(total=n_queries, seed=seed)
    labels = gold_labels()
    res = EvalResult(total=len(queries))
    rewrite_done = 0

    for i, item in enumerate(queries):
        gold = labels[item["name"]]["affected"]
        report = az.analyze(item["sql"], repeats=1)
        if report.error:
            res.errors.append({"sql": item["sql"], "error": report.error})
            continue
        flagged = _tool_says_affected(report)
        bucket = res.by_template.setdefault(item["name"], {
            "gold": gold, "flagged": 0, "total": 0})
        bucket["total"] += 1
        bucket["flagged"] += int(flagged)

        if gold and flagged:
            res.true_positive += 1
        elif not gold and flagged:
            res.false_positive += 1
        elif gold and not flagged:
            res.false_negative += 1
        else:
            res.true_negative += 1

        # 对受影响语句抽样做改写前后对比
        if flagged and rewrite_done < rewrite_sample:
            check = _compare_rewrite(db, az, rw, item["sql"])
            if check:
                res.rewrite_checks.append(check)
                rewrite_done += 1

    return res


def _compare_rewrite(db, az: Analyzer, rw: Rewriter, sql: str):
    """原语句 vs 改写语句：计划访问方式、耗时、结果逐行比对。"""
    original = az.analyze(sql, repeats=3)
    if original.error:
        return None
    try:
        sug = rw.suggest_for_query(sql)
    except Exception as exc:
        return {"sql": sql, "error": f"改写失败: {exc}"}

    equivalents = [s for s in sug["suggestions"] if s.get("equivalent")]
    chosen = equivalents[0] if equivalents else None
    entry = {
        "sql": sql,
        "original_access": [c.access for c in original.conditions],
        "original_ms": original.elapsed_ms,
        "original_rows": original.result_rows,
        "rewritten_sql": chosen["rewritten_sql"] if chosen else None,
        "equivalent": chosen["equivalent"] if chosen else None,
        "rewritten_access": None,
        "rewritten_ms": None,
        "rewritten_rows": None,
        "speedup": None,
        "non_equivalent_rewrites": [
            {"sql": s["rewritten_sql"], "diff": s["diff_detail"]}
            for s in sug["suggestions"] if s.get("equivalent") is False],
        "ddl": next((s.get("ddl") for s in sug["suggestions"] if s.get("ddl")), None),
        "ddl_lossless": next((s.get("ddl_lossless") for s in sug["suggestions"]
                              if s.get("ddl") is not None), None),
        "ddl_blockers": next((s.get("ddl_blockers") for s in sug["suggestions"]
                              if s.get("ddl") is not None), []),
    }
    if chosen:
        fixed = az.analyze(chosen["rewritten_sql"], repeats=3)
        entry["rewritten_access"] = [c.access for c in fixed.conditions]
        entry["rewritten_ms"] = fixed.elapsed_ms
        entry["rewritten_rows"] = fixed.result_rows
        if fixed.elapsed_ms > 0:
            entry["speedup"] = round(original.elapsed_ms / fixed.elapsed_ms, 2)
        # 逐行结果由 rewriter 内部 _compare_rows 保证，这里再独立复核一次
        try:
            r1 = az.execute_only(sql)
            r2 = az.execute_only(chosen["rewritten_sql"])
            entry["row_level_equal"] = sorted(map(repr, r1)) == sorted(map(repr, r2))
        except Exception as exc:
            entry["row_level_equal"] = f"复核异常: {exc}"
    return entry


def print_report(res: EvalResult):
    print("=" * 78)
    print(f"查询总数 {res.total} | 错误 {len(res.errors)}")
    print(f"TP={res.true_positive}  FP={res.false_positive}  "
          f"FN={res.false_negative}  TN={res.true_negative}")
    print(f"精确率 precision={res.precision:.4f}  召回率 recall={res.recall:.4f}  "
          f"F1={res.f1:.4f}")
    print("-" * 78)
    print("按模板：")
    for name, b in sorted(res.by_template.items()):
        tag = "OK " if (b["flagged"] > 0) == b["gold"] else "ERR"
        print(f"  [{tag}] {name:28s} gold={str(b['gold']):5s} "
              f"标出 {b['flagged']}/{b['total']}")
    print("-" * 78)
    print(f"改写前后对比抽样 {len(res.rewrite_checks)} 条：")
    n_eq = n_noneq = n_ddl = 0
    for c in res.rewrite_checks:
        if c.get("row_level_equal") is True:
            n_eq += 1
        if c.get("non_equivalent_rewrites"):
            n_noneq += 1
        if c.get("ddl"):
            n_ddl += 1
        sp = c.get("speedup")
        print(f"  原 {c['original_access']} {c['original_ms']:8.3f}ms "
              f"-> 改 {c['rewritten_access']} {c['rewritten_ms']}ms "
              f"提速 {sp}x | 逐行一致={c.get('row_level_equal')}")
        if c.get("non_equivalent_rewrites"):
            for ne in c["non_equivalent_rewrites"][:1]:
                print(f"      已拦截不等价改写: {ne['sql']} 差异={ne['diff']}")
    print(f"  其中：逐行一致 {n_eq} 条，拦截不等价改写 {n_noneq} 条，"
          f"附 DDL 建议 {n_ddl} 条")
    if res.errors:
        print("-" * 78)
        print("执行错误：")
        for e in res.errors[:10]:
            print(" ", e["sql"], "->", e["error"])


def save_report(res: EvalResult, path: str):
    data = asdict(res)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, default=str)
