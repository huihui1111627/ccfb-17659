# -*- coding: utf-8 -*-
"""端到端测试：在临时小库上验证分析、改写等价性、schema 评审、日志扫描。"""
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from icconv.analyzer import Analyzer
from icconv.catalog import Catalog
from icconv.log_scanner import LogScanner
from icconv.rewriter import Rewriter
from icconv.schema_review import SchemaReviewer
from icconv.storage import Database

DDL = """
CREATE TABLE users (
  id BIGINT PRIMARY KEY,
  phone VARCHAR(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci,
  KEY idx_phone (phone)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE orders (
  id BIGINT PRIMARY KEY,
  user_id VARCHAR(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin,
  KEY idx_user (user_id)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
"""


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("db"))
    cat = Catalog()
    cat.load_ddl(DDL)
    d = Database(path)
    d.init(cat)
    d.bulk_mode = True
    for i in range(1, 101):
        d.insert("users", {"id": i, "phone": f"138{i:08d}"})
    for i in range(1, 301):
        uid = (i % 100) + 1
        d.insert("orders", {"id": i, "user_id": f"{uid:04d}"})
    d.bulk_mode = False
    d._rebuild_all_indexes()
    d.flush()
    # 重新从磁盘加载，验证落盘往返
    d2 = Database(path)
    d2.load()
    return d2


class TestAnalysis:
    def test_string_column_number_literal_blocks_index(self, db):
        r = Analyzer(db).analyze("SELECT * FROM orders WHERE user_id = 7", repeats=1)
        assert not r.error
        assert r.has_index_block
        cond = r.conditions[0]
        assert cond.access == "seq_scan_blocked_by_coercion"
        assert cond.conversions[0]["on_column"] is True
        assert r.plan["kind"] == "seq_scan"
        assert r.plan["rows_examined"] == 300

    def test_number_column_string_literal_uses_index(self, db):
        r = Analyzer(db).analyze("SELECT * FROM users WHERE id = '7'", repeats=1)
        assert not r.has_index_block
        assert r.conditions[0].access == "index_seek"
        assert r.plan["rows_examined"] == 1

    def test_join_coercion(self, db):
        sql = ("SELECT u.id FROM users u JOIN orders o ON o.user_id = u.id "
               "WHERE u.id = 7")
        r = Analyzer(db).analyze(sql, repeats=1)
        join_cond = next(c for c in r.conditions if c.context == "join")
        assert join_cond.index_blocked is True

    def test_clean_query_not_flagged(self, db):
        r = Analyzer(db).analyze("SELECT * FROM users WHERE id = 7", repeats=1)
        assert not r.has_index_block
        assert not r.has_correctness_risk
        assert not r.has_potential_risk


class TestRewrite:
    def test_naive_quote_rewrite_rejected_as_non_equivalent(self, db):
        # orders.user_id 存的是 '0007'，加引号改成 '7' 会少行，必须被拦截
        out = Rewriter(db).suggest_for_query(
            "SELECT * FROM orders WHERE user_id = 7")
        sug = out["suggestions"][0]
        assert sug["equivalent"] is False
        assert sug["ddl"] is not None  # 同时给出 DDL 兜底

    def test_cast_literal_equivalent(self, db):
        out = Rewriter(db).suggest_for_query(
            "SELECT * FROM users WHERE id = '7'")
        sug = next(s for s in out["suggestions"] if s.get("equivalent"))
        before = Analyzer(db).execute_only("SELECT * FROM users WHERE id = '7'")
        after = Analyzer(db).execute_only(sug["rewritten_sql"])
        assert sorted(map(repr, before)) == sorted(map(repr, after))

    def test_ddl_lossless_check(self, db):
        # orders.user_id 全是定宽数字字符串，转 bigint 再转回会丢前导零 => 非无损
        rw = Rewriter(db)
        target = __import__("icconv.types", fromlist=["DataType"]).DataType("bigint")
        ok, blockers = rw.check_lossless("orders", "user_id", target)
        assert ok is False and blockers


class TestSchemaReview:
    def test_detects_type_family_mismatch(self, db):
        issues = SchemaReviewer(db).review()
        cats = {i.category for i in issues}
        assert "type_family_mismatch" in cats


class TestLogScanner:
    def test_scan_jsonl(self, db, tmp_path):
        log = tmp_path / "q.jsonl"
        import json
        rows = [
            {"ts": "2026-05-01T10:00:00", "sql": "SELECT * FROM orders WHERE user_id = 7",
             "exec_ms": 12.3},
            {"ts": "2026-05-01T10:01:00", "sql": "SELECT * FROM orders WHERE user_id = 8",
             "exec_ms": 14.1},
            {"ts": "2026-05-01T10:02:00", "sql": "SELECT * FROM users WHERE id = 7",
             "exec_ms": 0.4},
        ]
        log.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        scanner = LogScanner(db)
        affected = scanner.scan(scanner.parse_file(str(log)), simulate=False)
        fingerprints = {a.fingerprint for a in affected}
        assert any("user_id" in f and "?" in f for f in fingerprints)
        # 干净的主键查询不应出现
        assert all("users" not in f for f in fingerprints)

    def test_slow_log_parse(self, db, tmp_path):
        slow = tmp_path / "s.log"
        slow.write_text(
            "# Query_time: 12.5  Lock_time: 0\nSELECT * FROM orders WHERE user_id = 7;\n\n"
            "# Query_time: 0.3  Lock_time: 0\nSELECT * FROM users WHERE id = 7;\n",
            encoding="utf-8")
        entries = LogScanner(db).parse_file(str(slow))
        assert len(entries) == 2
        assert entries[0].exec_ms == 12.5
