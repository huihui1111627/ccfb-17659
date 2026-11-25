# -*- coding: utf-8 -*-
"""落盘存储引擎：JSONL 数据文件 + 内存有序索引。

设计要点：
* 每行严格按目录中的逻辑类型存储（Val 序列化），不依赖 SQLite 的动态类型，
  这样隐式转换语义完全由本工具自己掌控；
* 索引在加载时基于真实列值构建，支持等值、IN、范围、前缀 LIKE 的定位；
* 只有"探测值类型与列存储类型一致"时索引才能命中——planner 依据 coercion
  的分析结果决定走索引还是全表扫描，真实复现"转换落在列上 => 索引失效"。
"""
from __future__ import annotations

import bisect
import json
import os
from dataclasses import dataclass

from .catalog import Catalog, Index, Table
from .types import COLLATIONS, DataType
from .values import Val, convert


@dataclass
class Row:
    rowid: int
    values: list          # 与表列顺序一致的 Python 原生值

    def get(self, pos: int):
        return self.values[pos]


class Database:
    def __init__(self, path: str):
        self.path = path
        self.catalog = Catalog()
        self._rows: dict = {}          # table -> list[Row]
        self._eq_index: dict = {}      # (table, idx) -> {norm_key: [rowid]}
        self._sorted_index: dict = {}  # (table, idx) -> sorted [(norm_key, rowid)]
        self._loaded = False
        self.bulk_mode = False         # 批量装载时挂起逐行索引维护

    # ------------------------------------------------------------ 生命周期
    @property
    def catalog_path(self) -> str:
        return os.path.join(self.path, "catalog.json")

    def table_path(self, table: str) -> str:
        return os.path.join(self.path, "tables", f"{table}.jsonl")

    def init(self, catalog: Catalog):
        os.makedirs(os.path.join(self.path, "tables"), exist_ok=True)
        self.catalog = catalog
        for t in catalog.tables:
            self._rows[t] = []
        self._rebuild_all_indexes()
        self._loaded = True
        self.persist_catalog()

    def persist_catalog(self):
        self.catalog.save(self.catalog_path)

    def load(self):
        self.catalog = Catalog.load(self.catalog_path)
        for tname in self.catalog.tables:
            self._rows[tname] = []
            fpath = self.table_path(tname)
            if not os.path.exists(fpath):
                continue
            table = self.catalog.tables[tname]
            with open(fpath, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    raw = json.loads(line)
                    vals = [Val.from_json(item).value for item in raw]
                    self._rows[tname].append(Row(len(self._rows[tname]), vals))
            assert table is not None
        self._rebuild_all_indexes()
        self._loaded = True

    def flush_table(self, table: str):
        fpath = self.table_path(table)
        os.makedirs(os.path.dirname(fpath), exist_ok=True)
        t = self.catalog.tables[table]
        with open(fpath, "w", encoding="utf-8") as f:
            for row in self._rows[table]:
                out = [Val(v, t.columns[i].dtype).to_json()
                       for i, v in enumerate(row.values)]
                f.write(json.dumps(out, ensure_ascii=False, default=str) + "\n")

    def flush(self):
        for tname in self.catalog.tables:
            self.flush_table(tname)

    # ------------------------------------------------------------ 写入
    def insert(self, table: str, raw_values: dict):
        """raw_values: {列名: Python 原生值}，按列定义做一次类型规整。"""
        t = self.catalog.tables[table]
        rowid = len(self._rows[table])
        vals = []
        for col in t.columns:
            v = raw_values.get(col.name)
            if v is None:
                vals.append(None)
                continue
            if not isinstance(v, Val):
                v = Val(v, _guess_dtype(v))
            if v.dtype != col.dtype:
                v = convert(v, col.dtype)
            vals.append(v.value)
        row = Row(rowid, vals)
        self._rows[table].append(row)
        self._index_insert(table, row)
        return row

    def bulk_insert(self, table: str, rows: list):
        for r in rows:
            self.insert(table, r)

    # ------------------------------------------------------------ 读取
    def table(self, name: str) -> Table:
        return self.catalog.tables[name]

    def row_count(self, table: str) -> int:
        return len(self._rows[table])

    def scan(self, table: str):
        return iter(self._rows[table])

    def get_row(self, table: str, rowid: int) -> Row:
        return self._rows[table][rowid]

    # ------------------------------------------------------------ 索引
    def _norm_key(self, table: str, col_name: str, value):
        """排序规则归一化：ci 排序规则下字符串键小写化；探测值按列存储类型规整。"""
        if value is None:
            return None
        col = self.catalog.tables[table].column(col_name)
        # 探测值可能是与列不同族的原生类型（int vs str），索引键按列类型存，
        # 这里把探测值规整到列类型才能命中（前提：coercion 已确认无列侧转换）
        if col.dtype.is_integer and isinstance(value, str):
            value = convert(Val(value, DataType("varchar", length=len(value))),
                            col.dtype).value
        elif col.dtype.is_string and isinstance(value, (int, float)):
            value = convert(Val(value, _guess_dtype(value)), col.dtype).value
        if col.dtype.is_string and not COLLATIONS[col.dtype.collation]["case_sensitive"]:
            return str(value).lower()
        return value

    def _index_key(self, table: str, idx: Index, row: Row):
        t = self.catalog.tables[table]
        parts = []
        for cname in idx.columns:
            pos = next(i for i, c in enumerate(t.columns) if c.name == cname)
            parts.append(self._norm_key(table, cname, row.values[pos]))
        return tuple(parts)

    def _rebuild_all_indexes(self):
        self._eq_index.clear()
        self._sorted_index.clear()
        for tname, table in self.catalog.tables.items():
            for idx in table.indexes:
                eq, srt = {}, []
                for row in self._rows.get(tname, []):
                    key = self._index_key(tname, idx, row)
                    eq.setdefault(key, []).append(row.rowid)
                    srt.append((key, row.rowid))
                srt.sort(key=lambda kv: _sort_key(kv[0]))
                self._eq_index[(tname, idx.name)] = eq
                self._sorted_index[(tname, idx.name)] = srt

    def _index_insert(self, table: str, row: Row):
        if self.bulk_mode:
            return
        for idx in self.catalog.tables[table].indexes:
            key = self._index_key(table, idx, row)
            self._eq_index[(table, idx.name)].setdefault(key, []).append(row.rowid)
            srt = self._sorted_index[(table, idx.name)]
            sk = _sort_key(key)
            pos = bisect.bisect_left([_sort_key(k) for k, _ in srt], sk)
            srt.insert(pos, (key, row.rowid))

    def bulk_load(self):
        """进入批量装载模式：挂起逐行索引维护，退出时一次性重建全部索引。"""
        outer = self

        class _Bulk:
            def __enter__(self):
                outer.bulk_mode = True
                return outer

            def __exit__(self, *exc):
                outer.bulk_mode = False
                outer._rebuild_all_indexes()
                return False

        return _Bulk()

    def seek_eq(self, table: str, idx: Index, key_values: dict) -> list:
        """等值/IN 定位。key_values: {索引列名: 已归一化的原生值}，须覆盖索引前缀。"""
        full = self._probe_key(table, idx, key_values)
        if full is None:
            return []
        if isinstance(full, _PrefixHit):
            return list(full)
        return list(self._eq_index[(table, idx.name)].get(full, ()))

    def seek_range(self, table: str, idx: Index, key_values: dict,
                   op: str) -> list:
        """范围定位（>、>=、<、<=），仅支持单列范围谓词。"""
        srt = self._sorted_index[(table, idx.name)]
        sort_keys = [_sort_key(k) for k, _ in srt]
        probe = self._probe_key(table, idx, key_values)
        if probe is None:
            return []
        sk = _sort_key(probe)
        pos = bisect.bisect_left(sort_keys, sk)
        if op == ">=":
            picked = srt[pos:]
        elif op == ">":
            picked = srt[bisect.bisect_right(sort_keys, sk):]
        elif op == "<=":
            picked = srt[: bisect.bisect_right(sort_keys, sk)]
        elif op == "<":
            picked = srt[:pos]
        else:
            raise ValueError(op)
        return [rid for _, rid in picked]

    def seek_prefix(self, table: str, idx: Index, col: str, prefix: str) -> list:
        """前缀 LIKE 'abc%' 的范围定位。"""
        if idx.columns[0] != col:
            return []
        col_dtype = self.catalog.tables[table].column(col).dtype
        ci = not COLLATIONS[col_dtype.collation]["case_sensitive"]
        p = prefix.lower() if ci else prefix
        srt = self._sorted_index[(table, idx.name)]
        lo = bisect.bisect_left(
            [_sort_key((k[0],)) for k, _ in srt], _sort_key((p,)))
        out = []
        for key, rid in srt[lo:]:
            head = key[0]
            if head is None or not str(head).startswith(p):
                break
            out.append(rid)
        return out

    def _probe_key(self, table: str, idx: Index, key_values: dict):
        parts = []
        for cname in idx.columns:
            if cname not in key_values:
                break
            parts.append(self._norm_key(table, cname, key_values[cname]))
        if not parts:
            return None
        # 前缀键：eq_index 存的是完整键，需要前缀扫描 sorted 结构
        if len(parts) < len(idx.columns):
            srt = self._sorted_index[(table, idx.name)]
            prefix = tuple(parts)
            sk = _sort_key(prefix)
            sort_keys = [_sort_key(k[: len(prefix)]) for k, _ in srt]
            lo = bisect.bisect_left(sort_keys, sk)
            out = []
            for key, rid in srt[lo:]:
                if key[: len(prefix)] != prefix:
                    break
                out.append(rid)
            return _PrefixHit(out)
        return tuple(parts)


class _PrefixHit(tuple):
    """占位类型：前缀命中时直接携带 rowid 列表。"""


def _sort_key(key):
    """NULL 排最前，其余按原值，混合类型用类型标签兜底。"""
    out = []
    for v in key:
        if v is None:
            out.append((0, 0))
        elif isinstance(v, (int, float)):
            out.append((1, v))
        else:
            out.append((2, str(v)))
    return tuple(out)


def _guess_dtype(v) -> DataType:
    if isinstance(v, bool):
        return DataType("tinyint")
    if isinstance(v, int):
        return DataType("bigint")
    if isinstance(v, float):
        return DataType("double")
    from datetime import date, datetime
    if isinstance(v, datetime):
        return DataType("datetime")
    if isinstance(v, date):
        return DataType("date")
    return DataType("varchar", length=max(1, len(str(v))))
