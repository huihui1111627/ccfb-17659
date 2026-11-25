# -*- coding: utf-8 -*-
"""改写建议器。

两类改法：
1. 语句改写：把隐式转换挪到常量一侧（CAST/引号/COLLATE），改写后必须在模拟库里
   与原语句逐行比对，结果完全一致才会给出；
2. 列定义修改：先检查存量数据能否无损转换（前导零、非法日期、字符集不可编码…），
   给出 DDL 与检查结论，不能无损时明确列出阻碍数据。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from .analyzer import Analyzer
from .coercion import (
    RULE_COLLATION, RULE_NUMERIC_PROMOTION, RULE_STRING_TO_NUMBER,
    RULE_STRING_TO_TEMPORAL, RULE_TEMPORAL_TO_NUMBER,
)
from .parser import QueryParser
from .types import COLLATIONS, DataType
from .values import Val, convert


@dataclass
class RewriteSuggestion:
    condition: str
    issue: str
    rewritten_sql: str | None
    equivalent: bool | None
    diff_detail: dict | None
    ddl: str | None
    ddl_lossless: bool | None
    ddl_blockers: list = field(default_factory=list)
    approach: str = ""          # sql_rewrite / ddl_change / manual
    note: str = ""


class Rewriter:
    def __init__(self, db):
        self.db = db
        self.analyzer = Analyzer(db)

    @property
    def qp(self):
        # 每次访问返回全新 parser，避免跨查询缓存残留
        return QueryParser(self.db.catalog)

    # ================================================================ 语句改写
    def suggest_for_query(self, sql: str) -> dict:
        qp = self.qp
        qi = qp.parse(sql)
        report = self.analyzer.analyze(sql)
        suggestions = []
        for cond in report.conditions:
            if cond.rule == "NONE":
                continue
            pred = self._find_predicate(qi, cond.text)
            if pred is None:
                continue
            sug = self._rewrite_predicate(qp, qi, pred, cond)
            if sug:
                suggestions.append(sug.__dict__)
        return {
            "sql": sql,
            "suggestions": suggestions,
            "original_report": report.to_dict(),
        }

    def _find_predicate(self, qi, text: str):
        norm = " ".join(text.split()).lower()
        for p in qi.predicates:
            if " ".join(p.node.sql(dialect="mysql").split()).lower() == norm:
                return p
        return None

    def _rewrite_predicate(self, qp, qi, pred, cond) -> RewriteSuggestion | None:
        rule = cond.rule
        if rule == RULE_STRING_TO_NUMBER:
            return self._rewrite_string_number(qp, qi, pred, cond)
        if rule == RULE_STRING_TO_TEMPORAL:
            return self._rewrite_string_temporal(qp, qi, pred, cond)
        if rule == RULE_TEMPORAL_TO_NUMBER:
            return self._ddl_only(qp, qi, pred, cond,
                                  "日期列与数值比较无法通过改写常量保证语义一致，"
                                  "建议统一为日期类型或改应用传参")
        if rule == RULE_COLLATION:
            return self._rewrite_collation(qp, qi, pred, cond)
        if rule == RULE_NUMERIC_PROMOTION:
            return self._rewrite_numeric_promotion(qp, qi, pred, cond)
        return None

    # ---------- 字符串/数值
    def _rewrite_string_number(self, qp, qi, pred, cond) -> RewriteSuggestion:
        lo = qp.classify(pred.left_node, qi.tables)
        ro = qp.classify(pred.right_node, qi.tables)
        if lo.is_column and lo.dtype.is_string and ro.kind == "literal" and ro.dtype.is_numeric:
            # 数字常量加引号，转成字符串字面量，转换落在常量侧
            new_tree = qi.tree.copy()
            target_node = _locate_node(new_tree, pred.right_node)
            lit = exp.Literal.string(str(target_node.name))
            target_node.replace(lit)
            new_sql = new_tree.sql(dialect="mysql")
            return self._verify(qp, qi, pred, cond, new_sql,
                                "给数字常量加引号，使其成为字符串字面量")
        if ro.is_column and ro.dtype.is_string and lo.kind == "literal" and lo.dtype.is_numeric:
            new_tree = qi.tree.copy()
            target_node = _locate_node(new_tree, pred.left_node)
            target_node.replace(exp.Literal.string(str(target_node.name)))
            new_sql = new_tree.sql(dialect="mysql")
            return self._verify(qp, qi, pred, cond, new_sql,
                                "给数字常量加引号，使其成为字符串字面量")
        if lo.is_column and lo.dtype.is_numeric and ro.kind == "literal" and ro.dtype.is_string:
            # 字符串常量 CAST 成列的数值类型
            new_tree = qi.tree.copy()
            target_node = _locate_node(new_tree, pred.right_node)
            target_node.replace(_cast(target_node, lo.dtype))
            new_sql = new_tree.sql(dialect="mysql")
            return self._verify(qp, qi, pred, cond, new_sql,
                                f"把字符串常量显式 CAST 为 {lo.dtype.display}")
        if ro.is_column and ro.dtype.is_numeric and lo.kind == "literal" and lo.dtype.is_string:
            new_tree = qi.tree.copy()
            target_node = _locate_node(new_tree, pred.left_node)
            target_node.replace(_cast(target_node, ro.dtype))
            new_sql = new_tree.sql(dialect="mysql")
            return self._verify(qp, qi, pred, cond, new_sql,
                                f"把字符串常量显式 CAST 为 {ro.dtype.display}")
        # 两侧都是列（连接）：只能改 DDL
        return self._ddl_only(qp, qi, pred, cond,
                              "连接两侧都是列，语句改写无法消除列侧转换，需统一列类型")

    # ---------- 字符串/日期
    def _rewrite_string_temporal(self, qp, qi, pred, cond) -> RewriteSuggestion:
        lo = qp.classify(pred.left_node, qi.tables)
        ro = qp.classify(pred.right_node, qi.tables)
        col_op = lo if lo.is_column else ro
        lit_node = pred.right_node if lo.is_column else pred.left_node
        lit_op = ro if lo.is_column else lo
        if lit_op.kind == "literal" and lit_op.dtype.is_string:
            new_tree = qi.tree.copy()
            target_node = _locate_node(new_tree, lit_node)
            target_node.replace(_cast(target_node, DataType(col_op.dtype.kind)))
            new_sql = new_tree.sql(dialect="mysql")
            return self._verify(qp, qi, pred, cond, new_sql,
                                f"把字符串常量显式 CAST 为 {col_op.dtype.kind}")
        return self._ddl_only(qp, qi, pred, cond, "字符串日期列建议改为日期类型")

    # ---------- 排序规则
    def _rewrite_collation(self, qp, qi, pred, cond) -> RewriteSuggestion:
        lo = qp.classify(pred.left_node, qi.tables)
        ro = qp.classify(pred.right_node, qi.tables)
        # 常量侧显式 COLLATE 列的排序规则
        if lo.is_column and ro.kind == "literal":
            return self._collate_literal(qp, qi, pred, cond, pred.right_node, lo.dtype.collation)
        if ro.is_column and lo.kind == "literal":
            return self._collate_literal(qp, qi, pred, cond, pred.left_node, ro.dtype.collation)
        return self._ddl_only(qp, qi, pred, cond,
                              "两个列的字符集/排序规则不一致，需统一列定义")

    def _collate_literal(self, qp, qi, pred, cond, lit_node, collation):
        # 用 transform 把（可能已存在的）COLLATE 包装统一替换成目标排序规则，
        # 避免在树上按文本定位造成双层 COLLATE
        wanted = " ".join(lit_node.sql().split()).lower()

        def _replace(node):
            if isinstance(node, exp.Literal) and node.is_string and \
                    " ".join(node.sql().split()).lower() in wanted:
                return exp.Collate(
                    this=node.copy(),
                    expression=exp.Var(this=collation))
            if isinstance(node, exp.Collate) and \
                    " ".join(node.this.sql().split()).lower() in wanted:
                return exp.Collate(
                    this=node.this.copy(),
                    expression=exp.Var(this=collation))
            return node

        new_tree = qi.tree.copy().transform(_replace)
        new_sql = new_tree.sql(dialect="mysql")
        return self._verify(qp, qi, pred, cond, new_sql,
                            f"给常量加 COLLATE {collation}，转换落在常量侧")

    # ---------- 数值精度提升
    def _rewrite_numeric_promotion(self, qp, qi, pred, cond) -> RewriteSuggestion:
        lo = qp.classify(pred.left_node, qi.tables)
        ro = qp.classify(pred.right_node, qi.tables)
        # 仅当常量是被提升侧时可改写
        if ro.kind == "literal" and not ro.is_column:
            target_dtype = lo.dtype
            new_tree = qi.tree.copy()
            node = _locate_node(new_tree, pred.right_node)
            node.replace(_cast(node, target_dtype))
            return self._verify(qp, qi, pred, cond, new_tree.sql(dialect="mysql"),
                                f"常量显式 CAST 为 {target_dtype.display}")
        if lo.kind == "literal" and not lo.is_column:
            target_dtype = ro.dtype
            new_tree = qi.tree.copy()
            node = _locate_node(new_tree, pred.left_node)
            node.replace(_cast(node, target_dtype))
            return self._verify(qp, qi, pred, cond, new_tree.sql(dialect="mysql"),
                                f"常量显式 CAST 为 {target_dtype.display}")
        return self._ddl_only(qp, qi, pred, cond, "两列数值精度不一致，建议统一列类型")

    # ================================================================ 等价性校验
    def _verify(self, qp, qi, pred, cond, new_sql: str, approach: str) -> RewriteSuggestion:
        original_rows = self.analyzer.execute_only(qi.sql)
        try:
            new_rows = self.analyzer.execute_only(new_sql)
        except Exception as exc:
            sug = RewriteSuggestion(
                condition=cond.text,
                issue=cond.rule_text,
                rewritten_sql=new_sql, equivalent=False,
                diff_detail={"error": str(exc)}, ddl=None, ddl_lossless=None,
                approach="sql_rewrite", note=approach)
            return self._attach_ddl_fallback(qp, qi, pred, cond, sug)
        eq, diff = _compare_rows(original_rows, new_rows)
        sug = RewriteSuggestion(
            condition=cond.text, issue=cond.rule_text,
            rewritten_sql=new_sql, equivalent=eq, diff_detail=diff,
            ddl=None, ddl_lossless=None, approach="sql_rewrite", note=approach)
        if not eq:
            # 朴素改写与原语句结果不一致（典型：列里有 '0007' 这类带前导零的值），
            # 追加 DDL/数据治理建议，并明确为什么不能直接上线这个改写
            sug.note += ("；但该改写与原语句结果不一致，禁止直接上线。"
                         "根因通常是列中存在前导零/非数字后缀数据，隐式转换改变了匹配语义")
            self._attach_ddl_fallback(qp, qi, pred, cond, sug, as_fallback=True)
        return sug

    def _attach_ddl_fallback(self, qp, qi, pred, cond, sug, as_fallback=False):
        ddl_sug = self._ddl_only(qp, qi, pred, cond,
                                 "数据治理后统一列类型，才能在不改变结果的前提下用上索引")
        sug.ddl = ddl_sug.ddl
        sug.ddl_lossless = ddl_sug.ddl_lossless
        sug.ddl_blockers = ddl_sug.ddl_blockers
        return sug

    # ================================================================ DDL 建议
    def _ddl_only(self, qp, qi, pred, cond, note: str) -> RewriteSuggestion:
        cols = [(qp._resolve_table(t, qi.tables).name, c)
                for t, c in _columns_of(pred.node)]
        target = self._unified_dtype(cols)
        ddl, lossless, blockers = None, None, []
        if target is not None and cols:
            ddl_parts = []
            lossless = True
            for table_name, col_name in cols:
                table = self.db.table(table_name)
                col = table.column(col_name)
                ok, bad = self.check_lossless(table_name, col_name, target)
                if not ok:
                    lossless = False
                    blockers.extend(
                        [f"{table_name}.{col_name}: {b}" for b in bad[:5]])
                ddl_parts.append(
                    f"ALTER TABLE {table_name} MODIFY {col_name} {_ddl_type(target)};")
            ddl = "\n".join(ddl_parts)
        return RewriteSuggestion(
            condition=cond.text, issue=cond.rule_text,
            rewritten_sql=None, equivalent=None, diff_detail=None,
            ddl=ddl, ddl_lossless=lossless, ddl_blockers=blockers,
            approach="ddl_change", note=note)

    def _unified_dtype(self, cols):
        """决定统一后的目标类型：字符串编号->varchar(更长)，数值取高精度，字符集取超集。"""
        if not cols:
            return None
        dtypes = [self.db.table(t).column(c).dtype for t, c in cols]
        if any(d.is_string for d in dtypes):
            length = max((d.length or 1) for d in dtypes if d.is_string)
            # 字符集取优先级最高（utf8mb4 为超集）
            charset = min((d.charset for d in dtypes if d.is_string),
                          key=lambda cs: {"utf8mb4": 1, "utf8": 2, "gbk": 3, "latin1": 4}[cs])
            collation = next(d.collation for d in dtypes if d.charset == charset)
            return DataType("varchar", length=length, charset=charset, collation=collation)
        if all(d.is_numeric for d in dtypes):
            from .types import NUMERIC_RANK
            return max(dtypes, key=lambda d: NUMERIC_RANK[d.kind])
        if any(d.is_temporal for d in dtypes):
            return DataType("datetime")
        return dtypes[0]

    def check_lossless(self, table_name: str, col_name: str,
                       target: DataType) -> tuple:
        """检查存量数据转到目标类型是否无损，返回 (是否无损, 阻碍明细)。"""
        table = self.db.table(table_name)
        pos = next(i for i, c in enumerate(table.columns) if c.name == col_name)
        blockers, seen = [], set()
        for row in self.db.scan(table_name):
            v = row.values[pos]
            if v is None:
                continue
            out = convert(Val(v, table.columns[pos].dtype), target)
            for w in out.warnings:
                key = (w.code, str(v))
                if key not in seen:
                    seen.add(key)
                    blockers.append(f"值 {v!r}: {w.message}")
            # 回转校验：转过去再转回来必须相等
            if target.is_numeric and table.columns[pos].dtype.is_string:
                back = convert(out, table.columns[pos].dtype)
                if back.value is not None and str(back.value) != str(v):
                    key = ("roundtrip", str(v))
                    if key not in seen:
                        seen.add(key)
                        blockers.append(
                            f"值 {v!r}: 转为 {target.kind} 后回转得到 {back.value!r}，不可逆")
        return not blockers, blockers


def _cast(node: exp.Expression, dtype: DataType) -> exp.Cast:
    return exp.Cast(this=node.copy(),
                    to=sqlglot.parse_one(dtype.display.split()[0].upper(),
                                         into=exp.DataType))


def _ddl_type(dtype: DataType) -> str:
    base = dtype.display.split()[0]
    if dtype.is_string:
        return f"{base} CHARACTER SET {dtype.charset} COLLATE {dtype.collation}"
    return base


def _locate_node(tree: exp.Expression, original: exp.Expression) -> exp.Expression:
    """在复制后的树里按 SQL 文本定位对应节点。"""
    wanted = " ".join(original.sql().split()).lower()
    for n in tree.walk():
        if " ".join(n.sql().split()).lower() == wanted and type(n) is type(original):
            return n
    raise ValueError("改写后找不到目标节点")


def _columns_of(node: exp.Expression):
    for col in node.find_all(exp.Column):
        yield col.table, col.name


def _compare_rows(a: list, b: list) -> tuple:
    sa = sorted(map(repr, a))
    sb = sorted(map(repr, b))
    if sa == sb:
        return True, None
    only_a = [x for x in sa if x not in set(sb)]
    only_b = [x for x in sb if x not in set(sa)]
    return False, {
        "original_count": len(a), "rewritten_count": len(b),
        "only_in_original": only_a[:5], "only_in_rewritten": only_b[:5],
    }
