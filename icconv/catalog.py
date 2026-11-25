# -*- coding: utf-8 -*-
"""数据库目录：表、列、索引的元数据，支持从 MySQL 风格 DDL 解析。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from .types import DEFAULT_COLLATION, DataType, parse_type


@dataclass
class Index:
    name: str
    columns: list
    unique: bool = False
    primary: bool = False

    def to_dict(self) -> dict:
        return {"name": self.name, "columns": self.columns,
                "unique": self.unique, "primary": self.primary}

    @staticmethod
    def from_dict(d: dict) -> "Index":
        return Index(**d)


@dataclass
class Column:
    name: str
    dtype: DataType
    nullable: bool = True

    def to_dict(self) -> dict:
        return {"name": self.name, "dtype": self.dtype.to_dict(), "nullable": self.nullable}

    @staticmethod
    def from_dict(d: dict) -> "Column":
        return Column(d["name"], DataType.from_dict(d["dtype"]), d.get("nullable", True))


@dataclass
class Table:
    name: str
    columns: list
    indexes: list = field(default_factory=list)
    default_charset: str = "utf8mb4"
    default_collation: str | None = None

    def column(self, name: str) -> Column:
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError(f"表 {self.name} 没有列 {name}")

    def has_column(self, name: str) -> bool:
        return any(c.name == name for c in self.columns)

    def index_on(self, columns: list) -> Index | None:
        cols = tuple(columns)
        for idx in self.indexes:
            if tuple(idx.columns[: len(cols)]) == cols:
                return idx
        return None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "columns": [c.to_dict() for c in self.columns],
            "indexes": [i.to_dict() for i in self.indexes],
            "default_charset": self.default_charset,
            "default_collation": self.default_collation,
        }

    @staticmethod
    def from_dict(d: dict) -> "Table":
        return Table(
            d["name"],
            [Column.from_dict(c) for c in d["columns"]],
            [Index.from_dict(i) for i in d["indexes"]],
            d.get("default_charset", "utf8mb4"),
            d.get("default_collation"),
        )


class Catalog:
    def __init__(self):
        self.tables: dict = {}

    # ------------------------------------------------------------ DDL
    def load_ddl(self, ddl_text: str):
        for stmt in sqlglot.parse(ddl_text, read="mysql"):
            if stmt is None:
                continue
            if isinstance(stmt, exp.Create) and isinstance(stmt.this, exp.Schema):
                self._create_from_ddl(stmt)
            else:
                raise ValueError(f"仅支持 CREATE TABLE，收到：{type(stmt).__name__}")

    def _create_from_ddl(self, stmt: exp.Create):
        schema = stmt.this
        table_name = schema.this.name
        tbl_charset, tbl_collation = "utf8mb4", None
        for prop in stmt.args.get("properties") or []:
            if isinstance(prop, exp.CharacterSetProperty):
                tbl_charset = prop.name.lower()
            elif isinstance(prop, exp.CollateProperty):
                tbl_collation = prop.name.lower()
        if tbl_collation is None:
            tbl_collation = DEFAULT_COLLATION[tbl_charset]

        columns, indexes = [], []
        for item in schema.expressions:
            if isinstance(item, exp.ColumnDef):
                dtype_text = item.kind.sql(dialect="mysql").lower()
                charset, collation, nullable, pk, unsigned = tbl_charset, tbl_collation, True, False, False
                for con in item.args.get("constraints") or []:
                    csql = con.sql(dialect="mysql").lower()
                    if "primary key" in csql:
                        pk = True
                        nullable = False
                    elif "not null" in csql:
                        nullable = False
                    elif "character set" in csql:
                        charset = csql.split("character set")[1].strip().split()[0]
                    elif csql.startswith("collate"):
                        collation = csql.replace("collate", "").strip().split()[0]
                    if "unsigned" in csql:
                        unsigned = True
                full = dtype_text
                if "char" in dtype_text or "text" in dtype_text:
                    full += f" character set {charset} collate {collation}"
                if unsigned:
                    full += " unsigned"
                columns.append(Column(item.name, parse_type(full), nullable))
                if pk:
                    indexes.append(Index("PRIMARY", [item.name], unique=True, primary=True))
            else:
                # 表级索引/主键约束
                cls = type(item).__name__
                idx_cols = [c.name for c in item.args.get("expressions") or []]
                if not idx_cols and isinstance(item, exp.PrimaryKeyColumnConstraint):
                    idx_cols = [c.name for c in item.expressions]
                name = getattr(item, "name", None) or (
                    "PRIMARY" if "Primary" in cls else f"idx_{'_'.join(idx_cols)}")
                indexes.append(Index(
                    name, idx_cols,
                    unique=("Unique" in cls or "Primary" in cls),
                    primary="Primary" in cls,
                ))
        self.tables[table_name] = Table(table_name, columns, indexes,
                                        tbl_charset, tbl_collation)

    # ------------------------------------------------------------ 持久化
    def save(self, path: str):
        with open(path, "w", encoding="utf-8") as f:
            json.dump({n: t.to_dict() for n, t in self.tables.items()},
                      f, ensure_ascii=False, indent=1)

    @staticmethod
    def load(path: str) -> "Catalog":
        cat = Catalog()
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        cat.tables = {n: Table.from_dict(d) for n, d in data.items()}
        return cat
