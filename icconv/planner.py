# -*- coding: utf-8 -*-
"""计划器与计划执行：根据 coercion 分析决定索引是否可用，并真实跑数据计时。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from sqlglot import exp

from .coercion import decide
from .executor import EvalContext, Evaluator
from .parser import BetweenPredicate, InPredicate, Predicate, QueryParser, _split_and
from .values import Val, compare, convert


@dataclass
class PlanNode:
    kind: str                       # seq_scan / index_seek / nested_loop_join
    table: str
    detail: str = ""
    index_name: str | None = None
    rows_examined: int = 0
    rows_returned: int = 0
    elapsed_ms: float = 0.0
    children: list = field(default_factory=list)
    blocked_by: str | None = None   # 若该节点本可用索引但被转换挡住，记录原因

    def flatten(self) -> list:
        out = [self]
        for c in self.children:
            out.extend(c.flatten())
        return out


@dataclass
class PredAnalysis:
    """单个谓词的完整分析结果。"""
    predicate: Predicate
    rule: str
    rule_text: str
    steps: list
    blocked_sides: list
    potential_risks: list
    sargable: bool                  # 能否用上索引
    index_block_reason: str | None
    chosen_index: str | None
    runtime_warnings: list = field(default_factory=list)


class Planner:
    def __init__(self, db):
        self.db = db
        self.qp = QueryParser(db.catalog)

    # ================================================================ 分析
    def analyze_predicate(self, qi, pred: Predicate) -> PredAnalysis:
        if pred.op == "in":
            return self._analyze_in(qi, pred)
        if pred.op == "between":
            return self._analyze_between(qi, pred)
        if pred.op == "unknown":
            return PredAnalysis(pred, "UNSUPPORTED", "不支持的条件形态，按全表扫描处理",
                                [], [], [], False, "不支持的条件", None)
        lo = self.qp.classify(pred.left_node, qi.tables)
        ro = self.qp.classify(pred.right_node, qi.tables)
        decision = decide(lo, ro)
        sargable, reason, idx = self._check_index(qi, pred, lo, ro, decision)
        return PredAnalysis(
            pred, decision.rule, decision.rule_text,
            [s.__dict__ for s in decision.steps],
            decision.blocked_sides, list(decision.potential_risks),
            sargable, reason, idx,
        )

    def _analyze_in(self, qi, pred: InPredicate) -> PredAnalysis:
        lo = self.qp.classify(pred.left_node, qi.tables)
        decisions, all_steps, blocked = [], [], []
        for item in pred.right_node:
            ro = self.qp.classify(item, qi.tables)
            d = decide(lo, ro)
            decisions.append((ro, d))
            all_steps.extend(s.__dict__ for s in d.steps)
            blocked.extend(d.blocked_sides)
        first = decisions[0][1]
        sargable, reason, idx = self._check_index_in(qi, pred, lo, decisions)
        risks = []
        for _, d in decisions:
            risks.extend(d.potential_risks)
        return PredAnalysis(pred, first.rule, first.rule_text, all_steps,
                            sorted(set(blocked)), sorted(set(risks)),
                            sargable, reason, idx)

    def _analyze_between(self, qi, pred: BetweenPredicate) -> PredAnalysis:
        lo = self.qp.classify(pred.left_node, qi.tables)
        low_node, high_node = pred.right_node
        d1 = decide(lo, self.qp.classify(low_node, qi.tables))
        d2 = decide(lo, self.qp.classify(high_node, qi.tables))
        steps = [s.__dict__ for d in (d1, d2) for s in d.steps]
        blocked = sorted(set(d1.blocked_sides + d2.blocked_sides))
        # between 视为两个范围谓词，索引判定同范围
        fake = Predicate(pred.node, ">=", pred.left_node, low_node,
                         pred.context, pred.join_table)
        sargable, reason, idx = self._check_index(
            qi, fake, lo, self.qp.classify(low_node, qi.tables), d1)
        return PredAnalysis(pred, d1.rule, d1.rule_text, steps, blocked,
                            sorted(set(d1.potential_risks + d2.potential_risks)),
                            sargable, reason, idx)

    # ------------------------------------------------------------ 索引判定
    def _check_index(self, qi, pred, lo, ro, decision):
        # 找列在哪一侧
        col_side = None
        if lo.is_column:
            col_side, col_op, other_op = "left", lo, ro
        elif ro.is_column:
            col_side, col_op, other_op = "right", ro, lo
        if col_side is None:
            return False, "两侧都不是列，无法使用索引", None
        if pred.op not in ("=", "<>", "<", "<=", ">", ">=", "like"):
            return False, f"{pred.op.upper()} 条件不支持索引定位", None
        if col_side in decision.blocked_sides:
            return False, f"隐式转换落在列 {col_op.label()} 上，索引无法使用", None
        if pred.op == "like":
            # 仅前缀 LIKE 可用索引
            other_node = pred.right_node if col_side == "left" else pred.left_node
            if not isinstance(other_node, exp.Literal) or not other_node.is_string:
                return False, "LIKE 模式不是常量，无法使用索引", None
            if other_node.name.startswith("%") or other_node.name.startswith("_"):
                return False, "LIKE 以通配符开头，无法使用索引", None
        idx = self._find_index(col_op, pred.op)
        if idx is None:
            # 关键区分：没有索引是设计问题，不是隐式转换导致
            return False, None, None
        return True, None, idx.name

    def _check_index_in(self, qi, pred: InPredicate, lo, decisions):
        if not lo.is_column:
            return False, "IN 左侧不是列，无法使用索引", None
        if any("left" in d.blocked_sides for _, d in decisions):
            return False, f"隐式转换落在列 {lo.label()} 上，索引无法使用", None
        idx = self._find_index(lo, "=")
        if idx is None:
            return False, f"列 {lo.label()} 上没有可用索引", None
        return True, None, idx.name

    def _find_index(self, col_op, op: str):
        table = self.db.table(col_op.table)
        for idx in table.indexes:
            if idx.columns[0] == col_op.column:
                if op == "like":
                    return idx
                return idx
        return None

    def _real_table(self, qi, qualifier: str) -> str:
        return self.qp._resolve_table(qualifier, qi.tables).name

    def _pred_cols(self, qi, pred):
        return resolve_columns(pred, self.qp, qi)

    # ================================================================ 执行
    def run(self, qi, repeats: int = 3) -> tuple:
        """执行查询，返回 (结果行列表, 计划树, 运行时告警)。"""
        analyses = [self.analyze_predicate(qi, p) for p in qi.predicates]
        ctx = EvalContext(self.db, self.qp, qi)
        plan, rows = self._execute(qi, analyses, ctx, repeats)
        # 排序 / limit / 投影
        rows = self._apply_projection_order(qi, ctx, rows)
        return rows, plan, analyses, ctx.warnings

    def _execute(self, qi, analyses, ctx, repeats) -> tuple:
        if len(qi.tables) == 1:
            node, rows = self._run_single(qi, analyses, ctx, repeats)
            return node, rows
        return self._run_join(qi, analyses, ctx, repeats)

    def _preds_for(self, qi, analyses, table_name: str, context: str):
        out = []
        for a in analyses:
            p = a.predicate
            if p.context != context:
                continue
            cols = self._pred_cols(qi, p)
            if all(t == table_name for t, _ in cols):
                out.append(a)
        return out

    def _run_single(self, qi, analyses, ctx, repeats):
        tref = qi.tables[0]
        table_name = tref.name
        local = [a for a in analyses if a.predicate.context == "where"
                 and all(t == table_name for t, _ in self._pred_cols(qi, a.predicate))]
        # 选一个可走索引的等值/IN/范围谓词做驱动
        seek = next((a for a in local if a.sargable
                     and a.predicate.op in ("=", "in", "<", "<=", ">", ">=", "between", "like")), None)
        best, timings = None, []
        for _ in range(max(1, repeats)):
            if seek:
                candidate_ids = self._index_candidates(qi, seek)
                t0 = time.perf_counter()
                rows = self._filter_candidates(qi, ctx, table_name, candidate_ids)
                timings.append((time.perf_counter() - t0) * 1000)
                idx_name = seek.chosen_index
                node = PlanNode("index_seek", table_name,
                                f"索引 {idx_name} 定位（{seek.predicate.op}）",
                                index_name=idx_name, rows_examined=len(candidate_ids))
            else:
                t0 = time.perf_counter()
                rows = list(self._filter_scan(qi, ctx, table_name))
                timings.append((time.perf_counter() - t0) * 1000)
                reason = next((a.index_block_reason for a in local
                               if not a.sargable and a.index_block_reason), "无可用索引条件")
                node = PlanNode("seq_scan", table_name, f"全表扫描（{reason}）",
                                blocked_by=reason)
            best = node
        rows_returned = len(rows)
        node = best
        node.rows_examined = node.rows_examined if node.kind == "index_seek" \
            else self.db.row_count(table_name)
        node.rows_returned = rows_returned
        node.elapsed_ms = _median(timings)
        return node, rows

    def _run_join(self, qi, analyses, ctx, repeats):
        # 驱动表：行数最小的表（简化优化器）
        trefs = qi.tables
        outer = min(trefs, key=lambda t: self.db.row_count(t.name))
        inner = next(t for t in trefs if t is not outer)
        join_analysis = [a for a in analyses if a.predicate.context == "join"]
        outer_where = self._preds_for(qi, analyses, outer.name, "where")
        inner_where = self._preds_for(qi, analyses, inner.name, "where")

        timings = []
        for _ in range(max(1, repeats)):
            t0 = time.perf_counter()
            outer_rows = self._materialize_table(qi, ctx, outer.name, outer_where)
            # 内表连接谓词是否可走索引
            join_col_analysis = self._join_index_analysis(join_analysis, qi, inner.name)
            inner_node, joined = self._join_inner(
                qi, ctx, outer, inner, outer_rows,
                inner_where, join_col_analysis)
            timings.append((time.perf_counter() - t0) * 1000)

        outer_node = PlanNode(
            "seq_scan", outer.name,
            f"驱动表全表扫描 {self.db.row_count(outer.name)} 行",
            rows_examined=self.db.row_count(outer.name),
            rows_returned=len(outer_rows))
        join_node = PlanNode(
            "nested_loop_join", inner.name,
            f"嵌套循环连接 {outer.name} → {inner.name}",
            rows_returned=len(joined),
            elapsed_ms=_median(timings),
            children=[outer_node, inner_node])
        return join_node, joined

    def _join_inner(self, qi, ctx, outer, inner, outer_rows,
                    inner_where, join_col_analysis):
        # 找内表连接列
        join_pred = join_col_analysis.predicate if join_col_analysis else None
        can_index = bool(join_col_analysis and join_col_analysis.sargable)
        examined, result_rows = 0, []
        fast = self._build_fast_join(qi, join_pred) if join_pred else None
        hash_table = None
        if fast is not None and not can_index:
            # 预构建内表连接键哈希：内表连接列按 coercion 目标类型只转换一次，
            # 避免 O(外×内) 重复转换（字符集/字符串数值化连接尤其明显）
            hash_table = self._build_inner_hash(inner.name, outer.name, fast)
        ev = Evaluator(ctx)
        for orow in outer_rows:
            if can_index:
                # 先绑定外表行，_join_seek_ids 才能取到外表连接值
                ctx.bind({outer.name: orow})
                ids = self._join_seek_ids(qi, join_col_analysis, inner.name, ctx)
                examined += len(ids)
                for rid in ids:
                    irow = self.db.get_row(inner.name, rid)
                    ctx.bind({outer.name: orow, inner.name: irow})
                    if self._passes(qi, ctx, inner_where, ev) and \
                            self._passes_pred(qi, ctx, join_col_analysis.predicate, ev):
                        result_rows.append({outer.name: orow, inner.name: irow})
            elif hash_table is not None:
                ctx.bind({outer.name: orow})
                probe = self._outer_probe_key(outer.name, inner.name, fast, ctx)
                for rid in hash_table.get(probe, ()):
                    examined += 1
                    irow = self.db.get_row(inner.name, rid)
                    ctx.bind({outer.name: orow, inner.name: irow})
                    if self._passes(qi, ctx, inner_where, ev):
                        result_rows.append({outer.name: orow, inner.name: irow})
            elif fast is not None:
                # 快速路径：等值连接直接做带 coercion 的值比较，不重复走 AST
                outer_t, outer_c, inner_t, inner_c = self._fast_sides(
                    outer.name, inner.name, fast)
                for irow in self.db.scan(inner.name):
                    examined += 1
                    ctx.bind({outer.name: orow, inner.name: irow})
                    lv = Val(ctx.col_value(outer_t, outer_c),
                             self.db.table(outer_t).column(outer_c).dtype)
                    rv = Val(ctx.col_value(inner_t, inner_c),
                             self.db.table(inner_t).column(inner_c).dtype)
                    if lv.is_null or rv.is_null:
                        continue
                    lv2, rv2, warns = fast[4].apply(lv, rv)
                    if compare(lv2, "=", rv2):
                        ctx.add_warnings(warns)
                        if self._passes(qi, ctx, inner_where, ev):
                            result_rows.append({outer.name: orow, inner.name: irow})
            else:
                for irow in self.db.scan(inner.name):
                    examined += 1
                    ctx.bind({outer.name: orow, inner.name: irow})
                    if self._passes_pred(qi, ctx, join_pred, ev) and \
                            self._passes(qi, ctx, inner_where, ev):
                        result_rows.append({outer.name: orow, inner.name: irow})
        if can_index:
            node = PlanNode(
                "index_seek", inner.name,
                f"连接走索引 {join_col_analysis.chosen_index}（每次探测）",
                index_name=join_col_analysis.chosen_index,
                rows_examined=examined, rows_returned=len(result_rows))
        else:
            reason = (join_col_analysis.index_block_reason
                      if join_col_analysis and join_col_analysis.index_block_reason
                      else "连接列上没有可用索引" if join_col_analysis
                      else "没有连接条件")
            node = PlanNode(
                "seq_scan", inner.name,
                f"连接内表全表扫描（{reason}）", blocked_by=reason,
                rows_examined=examined, rows_returned=len(result_rows))
        return node, result_rows

    def _join_index_analysis(self, join_analysis, qi, inner_table):
        for a in join_analysis:
            cols = self._pred_cols(qi, a.predicate)
            if any(t == inner_table for t, _ in cols):
                return a
        return None

    def _build_fast_join(self, qi, pred: Predicate):
        """等值两列连接的快速描述：(表,列)×2 + 预决策的 coercion 结果。"""
        if pred.op != "=":
            return None
        lo = self.qp.classify(pred.left_node, qi.tables)
        ro = self.qp.classify(pred.right_node, qi.tables)
        if not (lo.is_column and ro.is_column):
            return None
        decision = decide(lo, ro)
        return (lo.table, lo.column, ro.table, ro.column, decision)

    def _fast_sides(self, outer_name: str, inner_name: str, fast):
        """返回 (外表表, 外表列, 内表表, 内表列)。"""
        left_t, left_c, right_t, right_c, decision = fast
        if left_t == outer_name and right_t == inner_name:
            return left_t, left_c, right_t, right_c
        if right_t == outer_name and left_t == inner_name:
            return right_t, right_c, left_t, left_c
        # 兜底：内表名匹配哪侧就用哪侧
        if right_t == inner_name:
            return left_t, left_c, right_t, right_c
        return right_t, right_c, left_t, left_c

    def _build_inner_hash(self, inner_table: str, outer_table: str, fast):
        """内表连接列一次性按 coercion 目标转换并建哈希。"""
        _, _, col_t, col_c = self._fast_sides(outer_table, inner_table, fast)
        left_t, left_c, right_t, right_c, decision = fast
        target = decision.right_target if (right_t, right_c) == (col_t, col_c) \
            else decision.left_target
        table = self.db.table(inner_table)
        pos = next(i for i, c in enumerate(table.columns) if c.name == col_c)
        out: dict = {}
        for row in self.db.scan(inner_table):
            raw = row.values[pos]
            if raw is None:
                continue
            v = Val(raw, table.columns[pos].dtype)
            if target is not None and v.dtype != target:
                v = convert(v, target)
            out.setdefault(_hashable(v.value), []).append(row.rowid)
        return out

    def _outer_probe_key(self, outer_table: str, inner_table: str, fast, ctx):
        col_t, col_c, _, _ = self._fast_sides(outer_table, inner_table, fast)
        left_t, left_c, right_t, right_c, decision = fast
        target = decision.left_target if (left_t, left_c) == (col_t, col_c) \
            else decision.right_target
        lv = Val(ctx.col_value(col_t, col_c),
                 self.db.table(col_t).column(col_c).dtype)
        if target is not None and lv.dtype != target:
            lv = convert(lv, target)
        return _hashable(lv.value)

    def _join_seek_ids(self, qi, analysis, inner_table, ctx):
        pred = analysis.predicate
        # 求另一侧（外表）当前值
        ev = Evaluator(ctx)
        if self.qp.classify(pred.left_node, qi.tables).table == inner_table:
            probe_val = ev.eval(pred.right_node)
            col_name = self.qp.classify(pred.left_node, qi.tables).column
        else:
            probe_val = ev.eval(pred.left_node)
            col_name = self.qp.classify(pred.right_node, qi.tables).column
        idx = self.db.table(inner_table).index_on([col_name])
        return self.db.seek_eq(inner_table, idx, {col_name: probe_val.value})

    def _materialize_table(self, qi, ctx, table_name, local_analyses):
        out = []
        for row in self.db.scan(table_name):
            ctx.bind({table_name: row})
            if self._passes(qi, ctx, local_analyses):
                out.append(row)
        return out

    def _index_candidates(self, qi, analysis: PredAnalysis):
        pred = analysis.predicate
        table_name = self._pred_cols(qi, pred)[0][0]
        table = self.db.table(table_name)
        left_op = self.qp.classify(pred.left_node, qi.tables)
        right_op = self.qp.classify(pred.right_node if pred.op != "in"
                                    else pred.right_node[0], qi.tables)
        if left_op.is_column:
            col_op, ev_node = left_op, pred.right_node
        else:
            col_op, ev_node = right_op, pred.left_node
        idx = table.index_on([col_op.column])
        # 无上下文求值常量
        ctx0 = EvalContext(self.db, self.qp, qi)
        ev = Evaluator(ctx0)
        if pred.op == "in":
            ids = set()
            for item in pred.right_node:
                v = ev.eval(item)
                ids.update(self.db.seek_eq(table_name, idx, {col_op.column: v.value}))
            return sorted(ids)
        if pred.op == "between":
            lo, hi = ev.eval(pred.right_node[0]), ev.eval(pred.right_node[1])
            # 常量按列类型规整（字符串日期 -> datetime 等），保证探测键同型
            col_dtype = table.column(col_op.column).dtype
            lo = convert(lo, col_dtype) if lo.dtype != col_dtype else lo
            hi = convert(hi, col_dtype) if hi.dtype != col_dtype else hi
            ids = set()
            for rid in self.db.seek_range(table_name, idx,
                                          {col_op.column: lo.value}, ">="):
                row = self.db.get_row(table_name, rid)
                pos = next(i for i, c in enumerate(table.columns) if c.name == col_op.column)
                if row.values[pos] is not None and row.values[pos] <= hi.value:
                    ids.add(rid)
            return sorted(ids)
        if pred.op == "like":
            v = ev.eval(ev_node)
            return self.db.seek_prefix(table_name, idx, col_op.column,
                                       v.value.split("%")[0])
        v = ev.eval(ev_node)
        kv = {col_op.column: v.value}
        if pred.op == "=":
            return self.db.seek_eq(table_name, idx, kv)
        return self.db.seek_range(table_name, idx, kv, pred.op)

    def _filter_candidates(self, qi, ctx, table_name, ids):
        out = []
        for rid in ids:
            row = self.db.get_row(table_name, rid)
            ctx.bind({table_name: row})
            if self._passes_all(qi, ctx):
                out.append(row)
        return out

    def _filter_scan(self, qi, ctx, table_name):
        for row in self.db.scan(table_name):
            ctx.bind({table_name: row})
            if self._passes_all(qi, ctx):
                yield row

    def _passes_all(self, qi, ctx, ev=None):
        where = qi.tree.find(exp.Where)
        if not where:
            return True
        return (ev or Evaluator(ctx)).eval(where.this) is True

    def _passes(self, qi, ctx, analyses, ev=None):
        e = ev or Evaluator(ctx)
        for a in analyses:
            if e.eval(a.predicate.node) is not True:
                return False
        return True

    def _passes_pred(self, qi, ctx, pred, ev=None):
        if pred is None:
            return True
        return (ev or Evaluator(ctx)).eval(pred.node) is True

    def _eval_condition(self, qi, ctx, node):
        return Evaluator(ctx).eval(node) is True

    # ------------------------------------------------------------ 投影/排序
    def _apply_projection_order(self, qi, ctx, rows):
        ev = Evaluator(ctx)
        projected = []
        for row in rows:
            if isinstance(row, dict):
                ctx.bind(row)
            else:
                ctx.bind({qi.tables[0].name: row})
            vals = []
            for expr in qi.projections:
                if isinstance(expr, exp.Star):
                    for t in qi.tables:
                        tname = t.name
                        r = row[tname] if isinstance(row, dict) else row
                        table = self.db.table(tname)
                        vals.extend(r.values[: len(table.columns)])
                    continue
                vals.append(ev.eval(expr).value)
            projected.append(tuple(vals))
        if qi.distinct:
            projected = list(dict.fromkeys(projected))
        if qi.order_by:
            def sort_key(vals):
                out = []
                for o in qi.order_by:
                    v = ev.eval(o) if not isinstance(o, exp.Column) else None
                    out.append((v is None, v))
                return tuple(out)
            # order 表达式在绑定上下文里求值
            keys = []
            for row in rows:
                ctx.bind(row if isinstance(row, dict) else {qi.tables[0].name: row})
                keys.append(tuple((ev.eval(o).value is None, ev.eval(o).value)
                                  for o in qi.order_by))
            order = qi.tree.find(exp.Ordered)
            descs = [o.args.get("desc") for o in (order.expressions if order else [])]
            idx = sorted(range(len(projected)),
                         key=lambda i: _cmp_key(keys[i], descs))
            projected = [projected[i] for i in idx]
        if qi.limit is not None:
            projected = projected[: qi.limit]
        return projected


def _predicate_columns(pred: Predicate) -> list:
    nodes = [pred.left_node]
    if pred.op == "in":
        nodes = [pred.left_node]
    elif pred.op == "between":
        nodes = [pred.left_node, *pred.right_node]
    elif not isinstance(pred.right_node, list):
        nodes.append(pred.right_node)
    out = []
    qi = pred.node.parent  # 不在此使用，限定名解析由调用方保证已带表名
    for n in nodes:
        for col in (n.find_all(exp.Column) if hasattr(n, "find_all") else []):
            out.append((col.table, col.name))
    return out


def resolve_columns(pred: Predicate, qp, qi) -> list:
    """把谓词里的 (别名, 列名) 解析成 (真实表名, 列名)。"""
    out = []
    for qualifier, cname in _predicate_columns(pred):
        tref = qp._resolve_table(qualifier, qi.tables)
        out.append((tref.name, cname))
    return out


def _median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2]


def _hashable(v):
    """把运行时值变成可做哈希键的形式（Decimal/date 统一转可比较原生类型）。"""
    from decimal import Decimal
    from datetime import date, datetime
    if isinstance(v, Decimal):
        return ("d", float(v))
    if isinstance(v, (datetime, date)):
        return ("t", v.isoformat())
    return ("v", v)


def _cmp_key(keys, descs):
    out = []
    for i, (is_null, v) in enumerate(keys):
        desc = descs[i] if i < len(descs) else False
        # NULL 排最前（ASC），其余按值；DESC 时翻转
        out.append((is_null, _Neg(v) if desc and v is not None else v))
    return tuple(out)


class _Neg:
    def __init__(self, v):
        self.v = v

    def __lt__(self, other):
        return self.v > other.v

    def __eq__(self, other):
        return self.v == other.v
