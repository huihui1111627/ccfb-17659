# -*- coding: utf-8 -*-
"""查询解析：sqlglot AST -> 内部查询表示，完成表别名绑定与操作数归类。"""
from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from .catalog import Catalog
from .coercion import Operand
from .types import DataType


@dataclass
class TableRef:
    name: str
    alias: str

    @property
    def key(self) -> str:
        return self.alias or self.name


@dataclass
class Predicate:
    """一个原子条件（WHERE 中 AND 拆开的叶子，或 JOIN ON 的等值条件）。"""
    node: exp.Expression
    op: str                       # = <> < <= > >= like in between and
    left_node: exp.Expression
    right_node: exp.Expression
    context: str                  # where / join
    join_table: str | None = None


@dataclass
class QueryInfo:
    sql: str
    tree: exp.Expression
    tables: list                  # TableRef
    predicates: list              # Predicate
    projections: list             # exp.Expression
    order_by: list
    limit: int | None
    distinct: bool


class QueryParser:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        self._classify_cache: dict = {}

    def parse(self, sql: str) -> QueryInfo:
        tree = sqlglot.parse_one(sql, read="mysql")
        if not isinstance(tree, exp.Select):
            raise ValueError("仅支持 SELECT 查询")
        # 缓存按查询隔离，避免不同 SQL 的节点 id 复用造成串数据
        self._classify_cache = {}
        tables = self._bind_tables(tree)
        preds = self._collect_predicates(tree, tables)
        limit = None
        lim = tree.args.get("limit")
        if lim is not None:
            limit = int(lim.expression.name)
        return QueryInfo(
            sql=sql, tree=tree, tables=tables, predicates=preds,
            projections=tree.expressions,
            order_by=[o.this for o in tree.args.get("order") or []],
            limit=limit, distinct=bool(tree.args.get("distinct")),
        )

    # ------------------------------------------------------------ 表绑定
    def _bind_tables(self, tree: exp.Select) -> list:
        refs = []
        for t in tree.find_all(exp.Table):
            refs.append(TableRef(t.name, t.alias_or_name))
        if not refs:
            raise ValueError("查询中没有表")
        return refs

    # ------------------------------------------------------------ 条件收集
    def _collect_predicates(self, tree: exp.Select, tables) -> list:
        preds = []
        where = tree.find(exp.Where)
        if where:
            for leaf in _split_and(where.this):
                p = self._to_predicate(leaf, "where", None)
                if p:
                    preds.append(p)
        for join in tree.find_all(exp.Join):
            on = join.args.get("on")
            jtable = join.this.alias_or_name
            if on:
                # on 本身就是比较表达式（EQ/LTE…）
                for leaf in _split_and(on):
                    p = self._to_predicate(leaf, "join", jtable)
                    if p:
                        preds.append(p)
        return preds

    def _to_predicate(self, node: exp.Expression, context: str,
                      join_table) -> Predicate | None:
        if isinstance(node, exp.EQ):
            op = "="
        elif isinstance(node, exp.NEQ):
            op = "<>"
        elif isinstance(node, exp.LT):
            op = "<"
        elif isinstance(node, exp.LTE):
            op = "<="
        elif isinstance(node, exp.GT):
            op = ">"
        elif isinstance(node, exp.GTE):
            op = ">="
        elif isinstance(node, exp.Like):
            op = "like"
        elif isinstance(node, exp.In):
            op = "in"
        elif isinstance(node, exp.Between):
            op = "between"
        else:
            return Predicate(node, "unknown", node, exp.Null(), context, join_table)
        if op == "in":
            left, right = node.this, node.expressions
            return InPredicate(node, "in", left, right, context, join_table)
        if op == "between":
            return BetweenPredicate(
                node, "between", node.this,
                (node.args.get("low"), node.args.get("high")),
                context, join_table)
        return Predicate(node, op, node.left, node.right, context, join_table)

    # ------------------------------------------------------------ 操作数归类
    def classify(self, node: exp.Expression, tables: list) -> Operand:
        """把一个表达式节点归类为 列/常量/表达式，并给出逻辑类型。"""
        cache = getattr(self, "_classify_cache", None)
        if cache is None:
            cache = self._classify_cache = {}
        key = id(node)
        cached = cache.get(key)
        if cached is not None:
            return cached
        result = self._classify_uncached(node, tables)
        cache[key] = result
        return result

    def _classify_uncached(self, node: exp.Expression, tables: list) -> Operand:
        if isinstance(node, exp.Column):
            tref = self._resolve_table(node.table, tables)
            table = self.catalog.tables[tref.name]
            col = table.column(node.name)
            return Operand(col.dtype, "column", tref.name, node.name, coercibility=2)
        if isinstance(node, exp.Collate):
            # 显式 COLLATE 的常量：coercibility=0（最强，列反而要迁就它）
            inner = self._literal(node.this)
            if inner is not None:
                coll = node.expression.name.lower()
                return Operand(inner.dtype.with_collation(coll),
                               "literal", coercibility=0)
        lit = self._literal(node)
        if lit is not None:
            return lit
        # 函数/算术表达式
        return Operand(self.infer_type(node, tables), "expr", coercibility=4)

    def _resolve_table(self, qualifier: str, tables: list) -> TableRef:
        if qualifier:
            for t in tables:
                if t.alias == qualifier or t.name == qualifier:
                    return t
            raise KeyError(f"未知表别名 {qualifier}")
        if len(tables) == 1:
            return tables[0]
        raise ValueError("多表查询中的列必须带表别名限定")

    def _literal(self, node: exp.Expression) -> Operand | None:
        if isinstance(node, exp.Literal):
            if node.is_string:
                coll = None
                parent = node.parent
                if isinstance(parent, exp.Collate):
                    coll = parent.expression.name.lower()
                dt = DataType("varchar", length=max(1, len(node.name)),
                              collation=coll) if coll else DataType(
                    "varchar", length=max(1, len(node.name)))
                return Operand(dt, "literal", coercibility=3)
            text = node.name
            if "." in text:
                return Operand(DataType("decimal",
                                        precision=len(text.replace("-", "")),
                                        scale=len(text.split(".")[1])),
                               "literal", coercibility=3)
            return Operand(DataType("bigint"), "literal", coercibility=3)
        if isinstance(node, exp.Null):
            return Operand(DataType("varchar", length=0), "literal",
                           coercibility=3)
        if isinstance(node, exp.Boolean):
            return Operand(DataType("tinyint"), "literal", coercibility=3)
        if isinstance(node, exp.Cast):
            inner = self._literal(node.this)
            return inner  # 显式 CAST 的常量按内部类型处理，coercibility 更低
        return None

    def infer_type(self, node: exp.Expression, tables: list) -> DataType:
        """推断表达式结果类型（简化）。"""
        if isinstance(node, exp.Cast):
            return _map_sqlglot_type(node.to)
        if isinstance(node, (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod)):
            lt = self.classify(node.left, tables).dtype
            rt = self.classify(node.right, tables).dtype
            if lt.is_float or rt.is_float:
                return DataType("double")
            if lt.is_decimal or rt.is_decimal:
                return DataType("decimal", precision=20, scale=4)
            return DataType("bigint")
        if isinstance(node, (exp.Upper, exp.Lower, exp.Concat, exp.Trim)):
            return DataType("varchar", length=255)
        if isinstance(node, (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max)):
            if isinstance(node, exp.Avg):
                return DataType("decimal", precision=20, scale=4)
            return DataType("decimal", precision=20, scale=0)
        return DataType("varchar", length=255)


@dataclass
class InPredicate(Predicate):
    pass


@dataclass
class BetweenPredicate(Predicate):
    pass


def _split_and(node: exp.Expression) -> list:
    if isinstance(node, exp.And):
        return _split_and(node.left) + _split_and(node.right)
    return [node]


def _map_sqlglot_type(sqlglot_type: exp.DataType) -> DataType:
    kind = sqlglot_type.this.name.lower()
    alias = {
        "varchar2": "varchar", "nvarchar": "varchar", "text": "text",
        "int2": "smallint", "int4": "int", "int8": "bigint",
        "bool": "tinyint", "boolean": "tinyint",
    }
    kind = alias.get(kind, kind)
    args = [a.name for a in sqlglot_type.expressions if hasattr(a, "name")]
    length = int(args[0]) if args and kind in ("char", "varchar") else None
    precision = int(args[0]) if len(args) >= 1 and kind == "decimal" else None
    scale = int(args[1]) if len(args) >= 2 and kind == "decimal" else None
    return DataType(kind, length=length, precision=precision, scale=scale)
