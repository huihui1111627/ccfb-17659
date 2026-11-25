# -*- coding: utf-8 -*-
"""表达式求值器：在真实数据上执行，隐式转换走 coercion + values 规则。"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlglot import exp

from .coercion import Operand, decide
from .types import DataType
from .values import Val, compare, convert
from . import parser as qparser


class EvalContext:
    def __init__(self, db, qp: "qparser.QueryParser", qi: "qparser.QueryInfo"):
        self.db = db
        self.qp = qp
        self.qi = qi
        # (table_name, column_name) -> 该列在当前行的 Val（已在 join 行里按位置取值）
        self.row_map: dict = {}
        self.warnings: list = []
        # 列位置缓存：(table, column) -> 列序号，避免每次 find
        self._col_pos = {}

    def bind(self, rows: dict):
        """rows: {逻辑表名: Row}"""
        self.row_map.clear()
        for tname, row in rows.items():
            table = self.db.table(tname)
            for i, col in enumerate(table.columns):
                self.row_map[(tname, col.name)] = Val(row.values[i], col.dtype)

    def col_value(self, table_name: str, col_name: str):
        """热路径：直接取当前绑定行的原生值。"""
        pos = self._col_pos.get((table_name, col_name))
        if pos is None:
            table = self.db.table(table_name)
            pos = next(i for i, c in enumerate(table.columns) if c.name == col_name)
            self._col_pos[(table_name, col_name)] = pos
        return self.row_map[(table_name, col_name)].value

    def add_warnings(self, warns):
        for w in warns:
            self.warnings.append(w)


class Evaluator:
    def __init__(self, ctx: EvalContext):
        self.ctx = ctx
        self.qp = ctx.qp
        self.qi = ctx.qi

    def eval(self, node: exp.Expression) -> Val:
        m = getattr(self, f"_ev_{type(node).__name__}", None)
        if m:
            return m(node)
        raise NotImplementedError(f"暂不支持表达式: {node.sql()} ({type(node).__name__})")

    # ------------------------------------------------------------ 基础节点
    def _ev_Column(self, node: exp.Column) -> Val:
        tref = self.qp._resolve_table(node.table, self.qi.tables)
        return self.ctx.row_map[(tref.name, node.name)]

    def _ev_Literal(self, node: exp.Literal) -> Val:
        op = self.qp._literal(node)
        if node.is_string:
            return Val(node.name, op.dtype)
        if "." in node.name:
            return Val(Decimal(node.name), op.dtype)
        return Val(int(node.name), op.dtype)

    def _ev_Null(self, node) -> Val:
        return Val(None, DataType("varchar", length=0))

    def _ev_Boolean(self, node) -> Val:
        return Val(1 if node.name.lower() == "true" else 0, DataType("tinyint"))

    def _ev_Paren(self, node) -> Val:
        return self.eval(node.this)

    def _ev_Neg(self, node) -> Val:
        v = self.eval(node.this)
        return Val(-v.value if v.value is not None else None, v.dtype)

    def _ev_Cast(self, node) -> Val:
        from .parser import _map_sqlglot_type
        target = _map_sqlglot_type(node.to)
        v = self.eval(node.this)
        out = convert(v, target)
        self.ctx.add_warnings(out.warnings)
        return Val(out.value, target)

    def _ev_Alias(self, node) -> Val:
        return self.eval(node.this)

    def _ev_Collate(self, node) -> Val:
        # 排序规则包装不改变运行时值，仅影响 coercion 层的归类
        return self.eval(node.this)

    # ------------------------------------------------------------ 算术
    def _arithmetic(self, node, fn):
        a = self.eval(node.left)
        b = self.eval(node.right)
        if a.is_null or b.is_null:
            return Val(None, DataType("double"))
        is_float = a.dtype.is_float or b.dtype.is_float or isinstance(node, exp.Div)
        x, y = float(a.value), float(b.value)
        try:
            r = fn(x, y)
        except ZeroDivisionError:
            return Val(None, DataType("double"))
        return Val(r, DataType("double" if is_float else "bigint"))

    def _ev_Add(self, node):
        return self._arithmetic(node, lambda x, y: x + y)

    def _ev_Sub(self, node):
        return self._arithmetic(node, lambda x, y: x - y)

    def _ev_Mul(self, node):
        return self._arithmetic(node, lambda x, y: x * y)

    def _ev_Div(self, node):
        return self._arithmetic(node, lambda x, y: x / y)

    def _ev_Mod(self, node):
        return self._arithmetic(node, lambda x, y: x % y)

    # ------------------------------------------------------------ 比较（走 coercion 引擎）
    def _compare(self, node, op: str):
        lv = self.eval(node.left)
        rv = self.eval(node.right)
        if lv.is_null or rv.is_null:
            return None
        lo = self.qp.classify(node.left, self.qi.tables)
        ro = self.qp.classify(node.right, self.qi.tables)
        decision = decide(lo, ro)
        lv2, rv2, warns = decision.apply(lv, rv)
        self.ctx.add_warnings(warns)
        return compare(lv2, op, rv2)

    def _ev_EQ(self, node):
        return self._compare(node, "=")

    def _ev_NEQ(self, node):
        r = self._compare(node, "=")
        return None if r is None else not r

    def _ev_LT(self, node):
        return self._compare(node, "<")

    def _ev_LTE(self, node):
        return self._compare(node, "<=")

    def _ev_GT(self, node):
        return self._compare(node, ">")

    def _ev_GTE(self, node):
        return self._compare(node, ">=")

    def _ev_Like(self, node):
        return self._compare(node, "like")

    def _ev_In(self, node):
        lv = self.eval(node.this)
        if lv.is_null:
            return None
        lo = self.qp.classify(node.this, self.qi.tables)
        matched = False
        for item in node.expressions:
            rv = self.eval(item)
            if rv.is_null:
                continue
            ro = self.qp.classify(item, self.qi.tables)
            decision = decide(lo, ro)
            a, b, warns = decision.apply(lv, rv)
            self.ctx.add_warnings(warns)
            if compare(a, "=", b):
                matched = True
                break
        return matched

    def _ev_Between(self, node):
        v = self.eval(node.this)
        low = self.eval(node.args.get("low"))
        high = self.eval(node.args.get("high"))
        if v.is_null or low.is_null or high.is_null:
            return None
        lo = self.qp.classify(node.this, self.qi.tables)
        r1 = self._coerced_compare(v, lo, low,
                                   self.qp.classify(node.args.get("low"), self.qi.tables), ">=")
        r2 = self._coerced_compare(v, lo, high,
                                   self.qp.classify(node.args.get("high"), self.qi.tables), "<=")
        if r1 is None or r2 is None:
            return None
        return r1 and r2

    def _coerced_compare(self, lv: Val, lo, rv: Val, ro, op: str):
        decision = decide(lo, ro)
        a, b, warns = decision.apply(lv, rv)
        self.ctx.add_warnings(warns)
        return compare(a, op, b)

    # ------------------------------------------------------------ 逻辑（三值）
    def _ev_And(self, node):
        a = self.eval(node.left)
        b = self.eval(node.right)
        if a is False or b is False:
            return False
        if a is None or b is None:
            return None
        return bool(a and b)

    def _ev_Or(self, node):
        a = self.eval(node.left)
        b = self.eval(node.right)
        if a is True or b is True:
            return True
        if a is None or b is None:
            return None
        return bool(a or b)

    def _ev_Not(self, node):
        v = self.eval(node.this)
        return None if v is None else not v

    def _ev_Is(self, node):
        v = self.eval(node.this)
        want_null = isinstance(node.expression, exp.Null)
        return v.is_null == want_null

    # ------------------------------------------------------------ 函数
    def _ev_Func(self, node) -> Val:
        name = node.sql_name().lower()
        args = [self.eval(a) for a in node.args.get("expressions", [])]
        if any(a.is_null for a in args):
            return Val(None, DataType("varchar", length=255))
        if name == "upper":
            return Val(str(args[0].value).upper(), args[0].dtype)
        if name == "lower":
            return Val(str(args[0].value).lower(), args[0].dtype)
        if name == "concat":
            return Val("".join(str(a.value) for a in args),
                       DataType("varchar", length=255))
        if name == "length" or name == "char_length":
            return Val(len(str(args[0].value)), DataType("int"))
        if name == "trim":
            return Val(str(args[0].value).strip(), args[0].dtype)
        if name == "substring":
            s = str(args[0].value)
            start = int(args[1].value) - 1
            length = int(args[2].value) if len(args) > 2 else None
            return Val(s[start: start + length] if length else s[start:],
                       args[0].dtype)
        if name == "coalesce":
            for a in [self.eval(x) for x in node.args.get("expressions", [])]:
                if not a.is_null:
                    return a
            return Val(None, DataType("varchar"))
        if name in ("now", "current_timestamp"):
            return Val(datetime.now(), DataType("datetime"))
        if name == "curdate":
            return Val(date.today(), DataType("date"))
        raise NotImplementedError(f"暂不支持函数 {name}")

    def _ev_Case(self, node) -> Val:
        for when in node.args.get("ifs", []):
            cond = self.eval(when.this)
            if cond is True:
                return self.eval(when.args["true"])
        default = node.args.get("default")
        if default is not None:
            return self.eval(default)
        return Val(None, DataType("varchar"))
