# -*- coding: utf-8 -*-
"""表结构评审：在上线前发现同名同义列的类型/字符集不一致。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .types import NUMERIC_RANK

# 业务同义列分组（真实场景可由数据字典/血缘平台提供，这里内置常见编号列）
SYNONYMS = {
    "user": ("user_id", "userid", "uid", "member_id", "customer_id"),
    "order": ("order_id", "orderid", "order_no", "order_number"),
    "product": ("product_id", "productid", "sku_id", "goods_id", "item_id"),
    "merchant": ("merchant_id", "shop_id", "seller_id", "store_id"),
    "phone": ("phone", "mobile", "telephone", "contact_phone"),
    "org": ("org_id", "dept_id", "department_id"),
}

# 表名（复数）-> 实体单数，用于外键式同义识别：orders.user_id ↔ users.id
TABLE_ENTITY = {
    "users": "user", "members": "user", "customers": "user",
    "orders": "order", "order": "order",
    "products": "product", "goods": "product", "items": "product",
    "merchants": "merchant", "shops": "merchant", "stores": "merchant",
    "categories": "category", "warehouses": "warehouse",
}

_DATE_NAME_RE = re.compile(r"(_at|_date|_time|date_|time_)", re.IGNORECASE)


@dataclass
class ReviewIssue:
    severity: str                   # critical / warning / info
    category: str
    title: str
    columns: list                   # [table.column ...]
    detail: str
    suggestion: str


@dataclass
class ColumnRef:
    table: str
    column: str
    dtype: object

    @property
    def full(self) -> str:
        return f"{self.table}.{self.column}"


class SchemaReviewer:
    def __init__(self, db):
        self.db = db

    def review(self) -> list:
        issues = []
        groups = self._group_columns()
        for key, refs in groups.items():
            if len({r.table for r in refs}) < 2:
                continue
            issues.extend(self._check_group(key, refs))
        issues.extend(self._check_date_as_string())
        return issues

    # ------------------------------------------------------------ 分组
    def _group_key(self, col_name: str) -> str:
        low = col_name.lower()
        for key, names in SYNONYMS.items():
            if low in names:
                return key
        return low  # 完全同名的列自然落到同一组

    def _group_columns(self):
        groups: dict = {}
        table_entity = {t: TABLE_ENTITY.get(t, t.rstrip("s"))
                        for t in self.db.catalog.tables}
        for tname, table in self.db.catalog.tables.items():
            for col in table.columns:
                key = self._group_key(col.name)
                # 外键式同义：本表的 <entity>_id 与实体主表的 id 归一组
                if col.name.lower().endswith("_id") and col.name.lower() != "id":
                    entity = col.name.lower()[:-3]
                    if any(e == entity for e in table_entity.values()):
                        key = entity
                groups.setdefault(key, []).append(
                    ColumnRef(tname, col.name, col.dtype))
            # 主表主键：users.id 归入 user 组
            entity = table_entity.get(tname)
            if entity and table.has_column("id"):
                pk = table.column("id")
                groups.setdefault(entity, []).append(
                    ColumnRef(tname, "id", pk.dtype))
        return groups

    def _group_key(self, col_name: str) -> str:
        low = col_name.lower()
        for key, names in SYNONYMS.items():
            if low in names:
                return key
        return low  # 完全同名的列自然落到同一组

    # ------------------------------------------------------------ 组内比对
    def _check_group(self, key: str, refs: list) -> list:
        issues = []
        families = {("numeric" if d.dtype.is_numeric else
                     "string" if d.dtype.is_string else
                     "temporal" if d.dtype.is_temporal else "other")
                    for d in refs}
        # 1) 整数与字符串混用——最严重，连接必然触发列侧转换
        if {"numeric", "string"}.issubset(families):
            issues.append(ReviewIssue(
                "critical", "type_family_mismatch",
                f"同义列 {key} 在不同表中数值/字符串类型混用",
                [r.full for r in refs],
                "；".join(f"{r.full} 为 {r.dtype.display}" for r in refs),
                "统一为同一类型族：编号类推荐 bigint（数据确有前导零等非数字字符时才用 varchar），"
                "先做存量数据无损检查再执行 ALTER"))
        # 2) 同族内具体类型/精度不一致
        numeric_refs = [r for r in refs if r.dtype.is_numeric]
        if len({r.dtype.kind for r in numeric_refs}) > 1:
            top = max(numeric_refs, key=lambda r: NUMERIC_RANK[r.dtype.kind])
            issues.append(ReviewIssue(
                "warning", "numeric_precision_mismatch",
                f"同义列 {key} 数值精度不一致",
                [r.full for r in numeric_refs],
                "；".join(f"{r.full} 为 {r.dtype.display}" for r in numeric_refs),
                f"统一提升为 {top.dtype.display}，避免连接时低精度侧重复转换"))
        # 3) 字符串列字符集/排序规则不一致
        str_refs = [r for r in refs if r.dtype.is_string]
        charsets = {r.dtype.charset for r in str_refs}
        collations = {r.dtype.collation for r in str_refs}
        if len(charsets) > 1:
            issues.append(ReviewIssue(
                "critical", "charset_mismatch",
                f"同义列 {key} 字符集不一致",
                [r.full for r in str_refs],
                "；".join(f"{r.full} 为 {r.dtype.charset}" for r in str_refs),
                "统一为 utf8mb4（超集），转换前确认目标字符集可编码全部存量数据"))
        elif len(collations) > 1:
            issues.append(ReviewIssue(
                "warning", "collation_mismatch",
                f"同义列 {key} 排序规则不一致",
                [r.full for r in str_refs],
                "；".join(f"{r.full} 为 {r.dtype.collation}" for r in str_refs),
                "统一排序规则（注意 bin 与 ci 的大小写敏感性差异会改变比较结果）"))
        # 4) 长度不一致（同字符集时提示）
        if len(charsets) == 1 and len({r.dtype.length for r in str_refs}) > 1:
            issues.append(ReviewIssue(
                "info", "length_mismatch",
                f"同义列 {key} 字符串长度不一致",
                [r.full for r in str_refs],
                "；".join(f"{r.full} 长度 {r.dtype.length}" for r in str_refs),
                "对齐长度，避免短列写入截断和跨表 JOIN 时的隐式长度转换"))
        return issues

    # ------------------------------------------------------------ 日期存成字符串
    def _check_date_as_string(self):
        issues = []
        for tname, table in self.db.catalog.tables.items():
            for col in table.columns:
                if col.dtype.is_string and _DATE_NAME_RE.search(col.name):
                    # 抽样验证数据是否真的长得像日期
                    sample = self._sample_values(tname, col.name, 20)
                    date_like = sum(1 for v in sample if _looks_like_date(v))
                    if sample and date_like / len(sample) >= 0.6:
                        issues.append(ReviewIssue(
                            "warning", "date_as_string",
                            f"{tname}.{col.name} 名称像日期但定义为字符串",
                            [f"{tname}.{col.name}"],
                            f"抽样 {len(sample)} 行中 {date_like} 行是日期格式字符串",
                            "改为 date/datetime 类型；与日期常量比较时当前会走字符串语义，"
                            "范围查询和索引都可能不符合预期"))
        return issues

    def _sample_values(self, table_name, col_name, n):
        table = self.db.table(table_name)
        pos = next(i for i, c in enumerate(table.columns) if c.name == col_name)
        out = []
        for row in self.db.scan(table_name):
            if row.values[pos] is not None:
                out.append(row.values[pos])
            if len(out) >= n:
                break
        return out


def _looks_like_date(v) -> bool:
    if not isinstance(v, str):
        return False
    return bool(re.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}", v) or
                re.match(r"^\d{8}([ T]\d{2}:?\d{2})?", v))
