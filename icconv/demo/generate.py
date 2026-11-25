# -*- coding: utf-8 -*-
"""演示库数据生成：真实分布 + 特意埋入的脏数据。"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from .schema import DEMO_DDL
from ..catalog import Catalog
from ..storage import Database

CITIES = ["北京", "上海", "广州", "深圳", "杭州", "成都", "武汉", "西安", "南京", "重庆"]
SURNAMES = list("赵钱孙李周吴郑王冯陈")
GIVEN = list("伟芳娜敏静丽强磊洋勇艳杰娟涛明超秀兰霞平刚桂英")
CHANNELS = ["支付宝", "微信支付", "银联", "花呗"]
ACTIONS = ["login", "create_order", "pay", "refund", "update_profile", "logout"]
RARE_CHARS = "𠮷𩽾𪚥㐂"  # utf8mb4 生僻字，gbk/latin1 无法编码


def _name(rng):
    return rng.choice(SURNAMES) + "".join(rng.sample(GIVEN, rng.randint(1, 2)))


def build_database(db_path: str, seed: int = 42, scale: float = 1.0) -> Database:
    rng = random.Random(seed)
    cat = Catalog()
    cat.load_ddl(DEMO_DDL)
    db = Database(db_path)
    db.init(cat)
    db.bulk_mode = True

    n_users = int(2000 * scale)
    n_merchants = int(200 * scale)
    n_products = int(800 * scale)
    n_orders = int(8000 * scale)

    dt0 = datetime(2025, 1, 1)

    def rdt():
        return dt0 + timedelta(seconds=rng.randint(0, 60 * 60 * 24 * 300))

    # ------------------------------------------------ users
    for i in range(1, n_users + 1):
        db.insert("users", {
            "id": i,
            "user_no": f"U{i:06d}",
            "name": _name(rng),
            "phone": f"1{rng.randint(30, 89)}{rng.randint(0, 999999999):09d}",
            "email": f"user{i}@example.com",
            "status": rng.randint(0, 2),
            "created_at": rdt(),
        })

    # ------------------------------------------------ merchants
    for i in range(1, n_merchants + 1):
        db.insert("merchants", {
            "id": i, "name": f"{rng.choice(CITIES)}{_name(rng)}店铺",
            "contact_phone": f"1{rng.randint(30, 89)}{rng.randint(0, 999999999):09d}",
            "rating": round(rng.uniform(3.0, 5.0), 2),
            "status": rng.randint(0, 1), "created_at": rdt(),
        })

    # ------------------------------------------------ categories / products
    for i in range(1, 51):
        db.insert("categories", {
            "id": i, "name": f"类目{i}", "parent_id": rng.choice([None, rng.randint(1, i)])})
    for i in range(1, n_products + 1):
        db.insert("products", {
            "id": i, "sku_id": 800000 + i,
            "name": f"商品{i}-{rng.choice(['旗舰版', '标准版', '青春版', '套装'])}",
            "category_id": rng.randint(1, 50),
            "price": round(rng.uniform(9.9, 9999.0), 2),
            "status": rng.randint(0, 2), "created_at": rdt(),
        })

    # ------------------------------------------------ orders：user_id 为带前导零的字符串
    for i in range(1, n_orders + 1):
        uid = rng.randint(1, n_users)
        # 70% 带前导零定宽，20% 纯数字字符串，10% 混入字母后缀（脏数据）
        roll = rng.random()
        if roll < 0.7:
            uid_str = f"{uid:06d}"
        elif roll < 0.9:
            uid_str = str(uid)
        else:
            uid_str = f"{uid}x"
        db.insert("orders", {
            "id": i, "user_id": uid_str,
            "merchant_id": rng.randint(1, n_merchants),
            "order_status": rng.randint(0, 4),
            "total_amount": round(rng.uniform(20, 5000), 2),
            "created_at": rdt(),
            "remark": rng.choice(["", "尽快发货", "工作日派送", f"含生僻字{RARE_CHARS}"]),
        })

    # ------------------------------------------------ order_items：order_id 字符串
    item_id = 1
    for oid in range(1, n_orders + 1):
        for _ in range(rng.randint(1, 3)):
            pid = rng.randint(1, n_products)
            db.insert("order_items", {
                "id": item_id, "order_id": str(oid),
                "product_id": pid, "quantity": rng.randint(1, 5),
                "price": round(rng.uniform(9.9, 2999.0), 2),
            })
            item_id += 1

    # ------------------------------------------------ payments
    for i in range(1, n_orders + 1):
        mid = rng.randint(1, n_merchants)
        db.insert("payments", {
            "id": i, "order_no": i,
            "merchant_id": str(mid),          # A 类：字符串 merchant_id
            "amount": round(rng.uniform(20, 5000), 2),
            "pay_channel": rng.randint(1, 4), "pay_status": rng.randint(0, 2),
            "paid_at": rdt(),
        })

    # ------------------------------------------------ refunds：order_id 低精度 int
    for i in range(1, int(n_orders * 0.08)):
        db.insert("refund_records", {
            "id": i, "order_id": rng.randint(1, n_orders),
            "refund_amount": round(rng.uniform(10, 800), 2),
            "reason": rng.choice(["质量问题", "七天无理由", "发错货", "重复下单"]),
            "created_at": rdt(),
        })

    # ------------------------------------------------ addresses
    for i in range(1, n_users + 1):
        for _ in range(rng.randint(1, 2)):
            db.insert("user_addresses", {
                "id": len(db._rows["user_addresses"]) + 1,
                "user_id": str(rng.randint(1, n_users)),
                "province": rng.choice(CITIES), "city": rng.choice(CITIES),
                "detail": f"{rng.randint(1, 200)}号{rng.randint(1, 30)}栋{rng.randint(101, 2503)}室",
                "is_default": rng.randint(0, 1),
            })

    # ------------------------------------------------ tickets：utf8 字符集
    for i in range(1, int(n_orders * 0.05)):
        db.insert("support_tickets", {
            "id": i, "user_id": f"{rng.randint(1, n_users):06d}",
            "title": rng.choice(["咨询发货", "退款咨询", "发票问题", "账号异常"]),
            "content": "工单内容" * rng.randint(2, 8),
            "status": rng.randint(0, 3), "created_at": rdt(),
        })

    # ------------------------------------------------ reviews：gbk，部分含 gbk 无法编码的生僻字
    for i in range(1, int(n_orders * 0.1)):
        uid = rng.randint(1, n_users)
        content = rng.choice(["好评", "物流很快", "包装一般", f"稀有评价{RARE_CHARS}"])
        db.insert("reviews", {
            "id": i, "customer_id": str(uid),
            "product_id": rng.randint(1, n_products),
            "score": rng.randint(1, 5), "content": content, "created_at": rdt(),
        })

    # ------------------------------------------------ inventory：latin1 字符串 product_id
    for i in range(1, n_products + 1):
        db.insert("inventory", {
            "id": i, "product_id": str(rng.randint(1, n_products)),
            "warehouse_id": rng.randint(1, 10), "stock": rng.randint(0, 5000),
            "updated_at": rdt(),
        })
    for i in range(1, 11):
        db.insert("warehouses", {"id": i, "name": f"{CITIES[(i-1) % 10]}仓",
                                 "city": CITIES[(i - 1) % 10]})

    # ------------------------------------------------ 字符串日期：格式混合 + 非法值
    date_fmts = [
        lambda d: d.strftime("%Y-%m-%d"),
        lambda d: d.strftime("%Y/%m/%d"),
        lambda d: d.strftime("%Y%m%d"),
    ]
    for i in range(1, 121):
        s, e = rdt(), rdt()
        fmt = rng.choice(date_fmts)
        db.insert("marketing_campaigns", {
            "id": i, "name": f"活动{i}",
            "start_date": fmt(s), "end_date": fmt(e),
            "budget": round(rng.uniform(1e4, 5e5), 2), "status": rng.randint(0, 2),
        })
    for i in range(1, 3001):
        d = rdt()
        db.insert("audit_logs", {
            "id": i, "operator_id": rng.randint(1, n_users),
            "action": rng.choice(ACTIONS),
            "target_table": rng.choice(["users", "orders", "payments", "products"]),
            "created_at": rng.choice(date_fmts)(d),
        })
    for i in range(1, 301):
        d1, d2 = rdt(), rdt()
        # 5% 非法日期
        vf = "not-a-date" if rng.random() < 0.05 else rng.choice(date_fmts)(d1)
        db.insert("coupons", {
            "id": i, "code": f"CP{i:06d}",
            "valid_from": vf, "valid_to": rng.choice(date_fmts)(d2),
            "discount": round(rng.uniform(5, 200), 2), "status": rng.randint(0, 2),
        })

    # ------------------------------------------------ 其余干净表（对照用，保证规模）
    for i in range(1, int(n_orders * 0.4)):
        db.insert("cart_items", {
            "id": i, "user_id": rng.randint(1, n_users),
            "product_id": rng.randint(1, n_products),
            "quantity": rng.randint(1, 3), "added_at": rdt()})
    for i in range(1, int(n_users * 0.8)):
        db.insert("favorites", {
            "id": i, "user_id": rng.randint(1, n_users),
            "product_id": rng.randint(1, n_products), "created_at": rdt()})
    for i in range(1, int(n_users * 1.2)):
        db.insert("messages", {
            "id": i, "from_user": rng.randint(1, n_users),
            "to_user": rng.randint(1, n_users),
            "content": f"消息{i}", "is_read": rng.randint(0, 1), "created_at": rdt()})
    for i in range(1, n_users + 1):
        db.insert("points_account", {
            "id": i, "user_id": i, "balance": rng.randint(0, 10000),
            "updated_at": rdt()})
    for i in range(1, int(n_orders * 0.3)):
        db.insert("points_log", {
            "id": i, "user_id": rng.randint(1, n_users),
            "change_points": rng.choice([-100, -50, 50, 100, 200]),
            "reason": rng.choice(["下单", "评价", "签到", "退款扣回"]), "created_at": rdt()})
    for i in range(1, int(n_orders * 0.6)):
        db.insert("shipping_orders", {
            "id": i, "order_id": i, "carrier": rng.choice(["顺丰", "圆通", "京东物流"]),
            "tracking_no": f"SF{rng.randint(10**9, 10**10 - 1)}",
            "status": rng.randint(0, 3), "shipped_at": rdt()})
    for i in range(1, int(n_orders * 0.2)):
        db.insert("search_history", {
            "id": i, "user_id": rng.randint(1, n_users),
            "keyword": rng.choice(["耳机", "手机壳", "咖啡", "键盘", "显示器"]),
            "searched_at": rdt()})
    for i in range(1, int(n_users * 0.5)):
        db.insert("notifications", {
            "id": i, "user_id": rng.randint(1, n_users),
            "msg_type": rng.randint(1, 4), "content": "您有一条新通知",
            "is_read": rng.randint(0, 1), "created_at": rdt()})
    for i in range(1, 21):
        db.insert("sys_config", {
            "id": i, "config_key": f"key_{i}",
            "config_value": f"value_{i}", "updated_at": rdt()})

    db.bulk_mode = False
    db._rebuild_all_indexes()
    db.flush()
    return db
