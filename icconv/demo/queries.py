# -*- coding: utf-8 -*-
"""演示查询生成与金标准标注。

每个模板带 label：
  affected=True  -> 金标准认为应被标出（存在隐式转换且有实际影响）
  affected=False -> 干净对照查询，不应被标出（用于统计误报）
weight 控制该模板在日志中出现的频次，模拟真实业务的热点语句。
"""
from __future__ import annotations

import random

# (模板名, SQL 模板, 是否受影响, 影响类型, 日志权重)
TEMPLATES = [
    # ===== A 类：字符串/数值混用 =====
    ("str_col_eq_num",
     "SELECT * FROM orders WHERE user_id = {uid}",
     True, "string_to_number_column", 12),
    ("str_col_in_nums",
     "SELECT id, total_amount FROM orders WHERE user_id IN ({uid}, {uid2}, {uid3})",
     True, "string_to_number_column", 6),
    ("num_col_eq_str",
     "SELECT * FROM users WHERE id = '{uid}'",
     True, "string_to_number_literal", 10),
    ("phone_as_number",
     "SELECT id, name FROM users WHERE phone = {phone}",
     True, "string_to_number_column", 8),
    ("join_str_bigint",
     "SELECT u.name, o.total_amount FROM users u "
     "JOIN orders o ON o.user_id = u.id WHERE u.id = {uid}",
     True, "join_string_number", 10),
    ("join_items_orders",
     "SELECT o.id, oi.product_id, oi.quantity FROM orders o "
     "JOIN order_items oi ON oi.order_id = o.id WHERE o.id = {oid}",
     True, "join_string_number", 6),
    ("join_payments_merchant",
     "SELECT m.name, p.amount FROM merchants m "
     "JOIN payments p ON p.merchant_id = m.id WHERE m.id = {mid}",
     True, "join_string_number", 5),
    ("join_address_user",
     "SELECT u.name, a.detail FROM users u "
     "JOIN user_addresses a ON a.user_id = u.id WHERE u.id = {uid}",
     True, "join_string_number", 4),
    ("str_col_range_num",
     "SELECT id FROM orders WHERE user_id > {uid} AND user_id < {uid2}",
     True, "string_to_number_column", 3),

    # ===== B 类：字符集/排序规则不一致 =====
    ("join_ticket_charset",
     "SELECT u.name, t.title FROM users u "
     "JOIN support_tickets t ON t.user_id = u.user_no WHERE u.id = {uid}",
     True, "charset_mismatch", 4),
    ("join_review_charset",
     "SELECT u.user_no, r.score FROM users u "
     "JOIN reviews r ON r.customer_id = u.user_no WHERE u.id = {uid}",
     True, "charset_mismatch", 4),
    ("collation_literal",
     "SELECT * FROM orders WHERE user_id = '000123' COLLATE utf8mb4_general_ci",
     True, "collation_mismatch", 3),
    ("join_inventory_latin1",
     "SELECT p.name, i.stock FROM products p "
     "JOIN inventory i ON i.product_id = p.id WHERE p.id = {pid}",
     True, "join_string_number_charset", 3),

    # ===== C 类：日期字符串（同型字符串比较，无隐式转换但语义脆弱，由评审/专项规则提示） =====
    ("date_str_col_eq",
     "SELECT * FROM marketing_campaigns WHERE start_date = '2025-03-01'",
     False, "string_date_design", 4),
    ("date_str_range",
     "SELECT id, name FROM marketing_campaigns WHERE start_date >= '2025-02-01' "
     "AND start_date <= '2025-06-30'",
     False, "string_date_design", 5),
    ("audit_str_range",
     "SELECT id, action FROM audit_logs WHERE created_at >= '20250301' "
     "AND created_at <= '20250331'",
     False, "string_date_design", 4),
    ("coupon_bad_date",
     "SELECT id, code FROM coupons WHERE valid_from = '2025-01-15'",
     False, "string_date_design", 3),
    ("real_date_compare",
     "SELECT id FROM orders WHERE created_at >= '2025-03-01 00:00:00' "
     "AND created_at <= '2025-03-31 23:59:59'",
     False, "clean_temporal_literal", 6),

    # ===== D 类：数值精度 =====
    ("join_refund_int_bigint",
     "SELECT o.id, rr.refund_amount FROM orders o "
     "JOIN refund_records rr ON rr.order_id = o.id WHERE o.id = {oid}",
     True, "numeric_promotion", 3),
    ("decimal_compare_int_lit",
     "SELECT * FROM payments WHERE amount > {amount}",
     False, "clean", 5),

    # ===== 干净对照（不应被标出） =====
    ("clean_pk_eq",
     "SELECT * FROM users WHERE id = {uid}", False, "clean", 14),
    ("clean_phone_str",
     "SELECT id, name FROM users WHERE phone = '13800001234'", False, "clean", 9),    ("clean_join",
     "SELECT u.name, c.product_id FROM users u "
     "JOIN cart_items c ON c.user_id = u.id WHERE u.id = {uid}",
     False, "clean", 8),
    ("clean_join_pay",
     "SELECT o.id, p.amount FROM orders o "
     "JOIN payments p ON p.order_no = o.id WHERE o.id = {oid}",
     False, "clean", 7),
    ("clean_name_like",
     "SELECT id, name FROM users WHERE name LIKE '张%'", False, "clean", 6),
    ("clean_in",
     "SELECT id, name FROM users WHERE id IN ({uid}, {uid2}, {uid3})",
     False, "clean", 7),
    ("clean_range",
     "SELECT id, total_amount FROM orders WHERE total_amount BETWEEN 100 AND 500",
     False, "clean_no_index", 5),
    ("clean_date_range",
     "SELECT id FROM orders WHERE created_at BETWEEN '2025-03-01 00:00:00' "
     "AND '2025-03-31 23:59:59'",
     False, "clean", 6),
    ("clean_status",
     "SELECT id FROM orders WHERE order_status = 1 AND merchant_id = {mid}",
     False, "clean", 5),    ("clean_product_join",
     "SELECT p.name, oi.quantity FROM products p "
     "JOIN order_items oi ON oi.product_id = p.id WHERE p.id = {pid}",
     False, "clean", 4),
]


def generate_queries(n_users=2000, n_merchants=200, n_products=800,
                     n_orders=8000, total=4000, seed=7):
    rng = random.Random(seed)
    weights = [t[4] for t in TEMPLATES]
    out = []
    for i in range(total):
        name, template, affected, kind, _ = rng.choices(TEMPLATES, weights=weights, k=1)[0]
        params = {
            "uid": rng.randint(1, n_users),
            "uid2": rng.randint(1, n_users),
            "uid3": rng.randint(1, n_users),
            "mid": rng.randint(1, n_merchants),
            "pid": rng.randint(1, n_products),
            "oid": rng.randint(1, n_orders),
            "phone": f"1{rng.randint(30, 89)}{rng.randint(0, 999999999):09d}",
            "amount": rng.randint(50, 3000),
        }
        out.append({
            "name": name,
            "sql": template.format(**params),
            "affected": affected,
            "kind": kind,
            "weight": dict((t[0], t[4]) for t in TEMPLATES)[name],
        })
    return out


def gold_labels():
    return {t[0]: {"affected": t[2], "kind": t[3]} for t in TEMPLATES}
