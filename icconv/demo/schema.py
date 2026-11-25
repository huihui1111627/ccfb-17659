# -*- coding: utf-8 -*-
"""演示业务库 DDL：电商场景 25 张表，故意埋入多类类型/字符集不一致。

埋点清单（与 labels.py 的标注对应）：
A. 整数/字符串混用编号
   - orders.user_id      varchar(32)  vs users.id bigint
   - order_items.order_id varchar(40) vs orders.id bigint
   - payments.merchant_id varchar(32) vs merchants.id bigint
   - user_addresses.user_id varchar(20) vs users.id bigint
B. 字符集/排序规则不一致的关联列
   - support_tickets.user_id utf8 vs users 相关列 utf8mb4
   - reviews.customer_id gbk vs users.id/name 体系
   - inventory.product_id latin1 vs products.id bigint（类型族也不同，双重问题）
C. 日期存成字符串
   - marketing_campaigns.start_date / end_date varchar
   - audit_logs.created_at varchar(20)
   - coupons.valid_from varchar（格式不统一：部分行写非法日期）
D. 数值精度不一致
   - refund_records.order_id 与 orders 同为数值但 int vs bigint
   - order_items.price decimal(10,2) vs payments.amount decimal(12,2)
E. 干净的同型对照（验证不误报）
   - orders.id / payments.order_no 均 bigint；products 体系内部全部一致
"""

DEMO_DDL = r"""
CREATE TABLE users (
  id BIGINT PRIMARY KEY,
  user_no VARCHAR(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci,
  name VARCHAR(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci,
  phone VARCHAR(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci,
  email VARCHAR(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci,
  status TINYINT,
  created_at DATETIME,
  KEY idx_phone (phone),
  KEY idx_name (name)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE merchants (
  id BIGINT PRIMARY KEY,
  name VARCHAR(80) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci,
  contact_phone VARCHAR(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci,
  rating DECIMAL(3,2),
  status TINYINT,
  created_at DATETIME
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE products (
  id BIGINT PRIMARY KEY,
  sku_id BIGINT,
  name VARCHAR(120) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci,
  category_id INT,
  price DECIMAL(10,2),
  status TINYINT,
  created_at DATETIME,
  KEY idx_sku (sku_id),
  KEY idx_category (category_id)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE categories (
  id INT PRIMARY KEY,
  name VARCHAR(60) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci,
  parent_id INT
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- A 类：user_id 用字符串，且部分值带前导零
CREATE TABLE orders (
  id BIGINT PRIMARY KEY,
  user_id VARCHAR(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin,
  merchant_id BIGINT,
  order_status TINYINT,
  total_amount DECIMAL(12,2),
  created_at DATETIME,
  remark VARCHAR(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci,
  KEY idx_orders_user (user_id),
  KEY idx_orders_merchant (merchant_id),
  KEY idx_orders_created (created_at)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- A 类：order_id 字符串 vs orders.id bigint
CREATE TABLE order_items (
  id BIGINT PRIMARY KEY,
  order_id VARCHAR(40) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci,
  product_id BIGINT,
  quantity INT,
  price DECIMAL(10,2),
  KEY idx_oi_order (order_id),
  KEY idx_oi_product (product_id)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- E 类干净对照：order_no bigint 与 orders.id 同型
CREATE TABLE payments (
  id BIGINT PRIMARY KEY,
  order_no BIGINT,
  -- A 类：merchant_id 字符串 vs merchants.id bigint
  merchant_id VARCHAR(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci,
  amount DECIMAL(12,2),
  pay_channel TINYINT,
  pay_status TINYINT,
  paid_at DATETIME,
  KEY idx_pay_order (order_no),
  KEY idx_pay_merchant (merchant_id)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- D 类：order_id int（精度低于 orders.id bigint）
CREATE TABLE refund_records (
  id BIGINT PRIMARY KEY,
  order_id INT,
  refund_amount DECIMAL(12,2),
  reason VARCHAR(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci,
  created_at DATETIME,
  KEY idx_refund_order (order_id)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- A 类：user_id 字符串（长度还与 orders.user_id 不同）
CREATE TABLE user_addresses (
  id BIGINT PRIMARY KEY,
  user_id VARCHAR(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci,
  province VARCHAR(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci,
  city VARCHAR(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci,
  detail VARCHAR(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci,
  is_default TINYINT,
  KEY idx_addr_user (user_id)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- B 类：user_id 用 utf8 字符集
CREATE TABLE support_tickets (
  id BIGINT PRIMARY KEY,
  user_id VARCHAR(32) CHARACTER SET utf8 COLLATE utf8_general_ci,
  title VARCHAR(120) CHARACTER SET utf8 COLLATE utf8_general_ci,
  content TEXT CHARACTER SET utf8 COLLATE utf8_general_ci,
  status TINYINT,
  created_at DATETIME,
  KEY idx_ticket_user (user_id)
) DEFAULT CHARSET=utf8 COLLATE=utf8_general_ci;

-- B 类：customer_id 用 gbk 字符集
CREATE TABLE reviews (
  id BIGINT PRIMARY KEY,
  customer_id VARCHAR(32) CHARACTER SET gbk COLLATE gbk_chinese_ci,
  product_id BIGINT,
  score TINYINT,
  content VARCHAR(500) CHARACTER SET gbk COLLATE gbk_chinese_ci,
  created_at DATETIME,
  KEY idx_review_customer (customer_id),
  KEY idx_review_product (product_id)
) DEFAULT CHARSET=gbk COLLATE=gbk_chinese_ci;

-- B 类双重问题：product_id latin1 字符串 vs products.id bigint
CREATE TABLE inventory (
  id BIGINT PRIMARY KEY,
  product_id VARCHAR(32) CHARACTER SET latin1 COLLATE latin1_swedish_ci,
  warehouse_id INT,
  stock INT,
  updated_at DATETIME,
  KEY idx_inv_product (product_id)
) DEFAULT CHARSET=latin1 COLLATE=latin1_swedish_ci;

CREATE TABLE warehouses (
  id INT PRIMARY KEY,
  name VARCHAR(60) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci,
  city VARCHAR(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- C 类：活动日期存成字符串
CREATE TABLE marketing_campaigns (
  id BIGINT PRIMARY KEY,
  name VARCHAR(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci,
  start_date VARCHAR(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci,
  end_date VARCHAR(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci,
  budget DECIMAL(12,2),
  status TINYINT,
  KEY idx_camp_start (start_date)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- C 类：审计时间存字符串
CREATE TABLE audit_logs (
  id BIGINT PRIMARY KEY,
  operator_id BIGINT,
  action VARCHAR(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci,
  target_table VARCHAR(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci,
  created_at VARCHAR(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci,
  KEY idx_audit_created (created_at)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- C 类：券有效期字符串，且混入非法格式
CREATE TABLE coupons (
  id BIGINT PRIMARY KEY,
  code VARCHAR(40) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci,
  valid_from VARCHAR(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci,
  valid_to VARCHAR(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci,
  discount DECIMAL(8,2),
  status TINYINT,
  KEY idx_coupon_valid (valid_from)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE cart_items (
  id BIGINT PRIMARY KEY,
  user_id BIGINT,
  product_id BIGINT,
  quantity INT,
  added_at DATETIME,
  KEY idx_cart_user (user_id)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE favorites (
  id BIGINT PRIMARY KEY,
  user_id BIGINT,
  product_id BIGINT,
  created_at DATETIME,
  UNIQUE KEY uk_fav (user_id, product_id)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE messages (
  id BIGINT PRIMARY KEY,
  from_user BIGINT,
  to_user BIGINT,
  content VARCHAR(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci,
  is_read TINYINT,
  created_at DATETIME,
  KEY idx_msg_to (to_user)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE points_account (
  id BIGINT PRIMARY KEY,
  user_id BIGINT,
  balance INT,
  updated_at DATETIME,
  UNIQUE KEY uk_points_user (user_id)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE points_log (
  id BIGINT PRIMARY KEY,
  user_id BIGINT,
  change_points INT,
  reason VARCHAR(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci,
  created_at DATETIME,
  KEY idx_plog_user (user_id)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE shipping_orders (
  id BIGINT PRIMARY KEY,
  order_id BIGINT,
  carrier VARCHAR(40) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci,
  tracking_no VARCHAR(60) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci,
  status TINYINT,
  shipped_at DATETIME,
  KEY idx_ship_order (order_id)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE search_history (
  id BIGINT PRIMARY KEY,
  user_id BIGINT,
  keyword VARCHAR(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci,
  searched_at DATETIME,
  KEY idx_search_user (user_id)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE notifications (
  id BIGINT PRIMARY KEY,
  user_id BIGINT,
  msg_type TINYINT,
  content VARCHAR(300) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci,
  is_read TINYINT,
  created_at DATETIME,
  KEY idx_notif_user (user_id)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE sys_config (
  id BIGINT PRIMARY KEY,
  config_key VARCHAR(60) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci,
  config_value VARCHAR(300) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci,
  updated_at DATETIME
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
"""
