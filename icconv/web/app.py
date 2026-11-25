# -*- coding: utf-8 -*-
"""Flask Web 应用：单条查询转换分析 + 全库列一致性清单。"""
from __future__ import annotations

import os

from flask import Flask, jsonify, render_template, request

from ..analyzer import Analyzer
from ..rewriter import Rewriter
from ..schema_review import SchemaReviewer
from ..storage import Database


def create_app(db_path: str) -> Flask:
    app = Flask(__name__)
    db = Database(db_path)
    db.load()

    def services():
        # 每次请求新建分析器，保证状态隔离
        return Analyzer(db), Rewriter(db), SchemaReviewer(db)

    # ------------------------------------------------------------ 页面
    @app.route("/")
    def index():
        return render_template("index.html",
                               tables=sorted(db.catalog.tables.keys()))

    @app.route("/schema")
    def schema_page():
        _, _, reviewer = services()
        issues = reviewer.review()
        return render_template("schema.html", issues=issues)

    # ------------------------------------------------------------ API
    @app.route("/api/analyze", methods=["POST"])
    def api_analyze():
        sql = (request.json or {}).get("sql", "").strip()
        if not sql:
            return jsonify({"error": "SQL 不能为空"}), 400
        az, rw, _ = services()
        report = az.analyze(sql, repeats=3)
        if report.error:
            return jsonify({"error": report.error})
        try:
            suggestions = rw.suggest_for_query(sql)["suggestions"]
        except Exception as exc:
            suggestions = [{"error": f"改写分析失败: {exc}"}]
        return jsonify({
            "report": report.to_dict(),
            "suggestions": suggestions,
        })

    @app.route("/api/schema")
    def api_schema():
        _, _, reviewer = services()
        return jsonify({"issues": [i.__dict__ for i in reviewer.review()]})

    @app.route("/api/table/<name>")
    def api_table(name):
        if name not in db.catalog.tables:
            return jsonify({"error": "表不存在"}), 404
        t = db.table(name)
        return jsonify({
            "name": name,
            "row_count": db.row_count(name),
            "columns": [c.to_dict() for c in t.columns],
            "indexes": [i.to_dict() for i in t.indexes],
        })

    return app
