# 隐式类型转换分析工具（icconv）

一个自带简化数据库、可真实执行 SQL 的隐式转换分析平台。表结构、列类型、字符集、
排序规则、索引都可定义，数据真正落盘；比较与连接时的隐式转换规则参照 MySQL 8.0
的真实行为实现，转换落在哪一侧决定了索引能不能用。

## 它解决什么问题

线上一条查询从几毫秒变成十几秒，执行计划显示全表扫描——索引明明在。根因往往是
连接/比较两侧类型不一致，数据库把**列**上的每一行都做了隐式转换：

- 订单表 `user_id VARCHAR` 对用户表 `id BIGINT`：字符串列每行转数字，索引失效；
- 两张关联表字符集/排序规则不同（utf8mb4 vs gbk、bin vs ci）；
- 应用把手机号当数字传进来查字符串列；
- 日期存成字符串，比较语义和范围查询行为偏离预期。

本工具把这类问题在**开发评审、日志扫描、单条分析**三个环节都暴露出来，并给出
经过逐行等价性验证的改法。

## 核心能力

1. **真实的隐式转换规则引擎**（`icconv/coercion.py`）
   - 字符串↔数值：MySQL 铁律——字符串一侧转数值；区分转换落在列上还是常量上；
   - 不同精度数值：列-列按精度提升，列-常量时常量迁就列（不挡索引）；
   - 日期↔字符串、日期↔数值；
   - 不同字符集/排序规则：coercibility（列强于常量、显式 COLLATE 最强）+ 优先级；
   - 每次决策明确给出：转哪一侧、目标类型、是否导致该列索引失效、潜在正确性风险。
2. **单查询分析**（`icconv/analyzer.py`）：逐条件标注规则、转换步骤、索引可用性、
   实际执行告警（前导零丢失、非数字后缀截断、空串变 0、非法日期、字符集乱码）。
3. **可执行的落盘数据库**（`icconv/storage.py`）：JSONL 数据 + 内存有序索引，
   只有探测值与列存储类型一致时索引才能命中，真实复现"转换挡索引"；执行计划区分
   `index_seek / seq_scan_blocked_by_coercion / seq_scan_no_index`。
4. **改写与等价性校验**（`icconv/rewriter.py`）
   - 语句改写把转换挪到常量侧（加引号 / CAST / COLLATE）；
   - 每条改写都在模拟库执行并与原语句**逐行比对**，不一致直接拦截（例如列里有
     `'0007'` 时把 `user_id = 7` 改成 `user_id = '7'` 会少行，禁止上线）；
   - 列定义修改先做**存量数据无损检查**（前导零、非法日期、不可编码字符、回转校验），
     给出 ALTER DDL 和阻碍明细。
5. **日志扫描**（`icconv/log_scanner.py`）：解析 JSONL 与慢日志，参数化指纹聚合，
   按执行次数、累计耗时、实测改写前后的多花时间排序。
6. **表结构评审**（`icconv/schema_review.py`）：同名同义列（含外键式
   `orders.user_id` ↔ `users.id`）的类型族、精度、字符集、排序规则、长度不一致预警，
   以及"日期存成字符串"识别。
7. **Web 页面**（`icconv/web`）：输入语句看逐条件转换分析、执行计划、改写建议，
   以及全库列不一致清单。

## 快速开始

```bash
pip install -r requirements.txt

# 1. 构建演示库：25 张表、约 6 万行，埋入全部不一致类型与脏数据
python -m icconv.demo.cli build --path ./demo_data

# 2. 表结构评审
python -m icconv.demo.cli review --path ./demo_data

# 3. 端到端评估：生成查询，统计漏标/误报，并对改写做前后计划、耗时、逐行对比
python -m icconv.demo.cli evaluate --path ./demo_data --queries 1500 --out eval.json

# 4. 扫描几个月的查询日志（不带 --logs 会自动生成示例日志）
python -m icconv.demo.logscan --path ./demo_data --gen 4000 --top 20

# 5. 启动 Web
python -m icconv.web.run --path ./demo_data
# 打开 http://127.0.0.1:5000
```

## 演示库埋点（`icconv/demo/schema.py`）

| 类别 | 例子 |
|---|---|
| 整数/字符串混用编号 | `orders.user_id VARCHAR` vs `users.id BIGINT`（含前导零、字母后缀脏数据） |
| 字符集/排序规则冲突 | `support_tickets.user_id` utf8、`reviews.customer_id` gbk、`orders.user_id` bin |
| 双重问题 | `inventory.product_id` latin1 字符串 vs `products.id BIGINT` |
| 日期存成字符串 | `marketing_campaigns.start_date`、`audit_logs.created_at`、含非法日期的 `coupons.valid_from` |
| 数值精度不一致 | `refund_records.order_id INT` vs `orders.id BIGINT` |
| 干净对照 | `payments.order_no BIGINT` 等，用于检验误报 |

查询模板（`icconv/demo/queries.py`）共 30 个，其中 19 个金标准受影响、11 个干净对照，
按权重生成几千条真实写法的查询。

## 评估结果（1500 条查询）

```
TP=680  FP=0  FN=0  TN=820
precision=1.0000  recall=1.0000  F1=1.0000
```

- 被拦截的不等价改写：朴素加引号导致前导零数据漏匹配，工具明确拒绝并附 DDL 治理建议；
- 等价改写后访问路径从全表扫描变为索引定位，单语句耗时下降可达 5 个数量级；
- 干净对照查询（同型比较、常量侧无害转换、无索引但无转换）零误报。

## 代码结构

```
icconv/
  types.py          逻辑类型、字符集、排序规则
  values.py         带类型运行时值 + MySQL 风格转换/比较（含告警）
  coercion.py       隐式转换规则引擎（核心）
  catalog.py        DDL 解析与元数据目录
  storage.py        JSONL 落盘存储 + 内存索引（等值/范围/前缀）
  parser.py         sqlglot AST -> 内部查询表示，操作数归类
  executor.py       表达式求值（三值逻辑、走 coercion 引擎）
  planner.py        计划器：索引判定、嵌套循环/哈希连接、真实执行计时
  analyzer.py       单查询分析报告
  rewriter.py       改写建议 + 逐行等价校验 + DDL 无损检查
  schema_review.py  全库列一致性评审
  log_scanner.py    日志解析、指纹聚合、影响排序
  evaluate.py       漏标/误标评估与改写前后对比
  demo/             演示库 DDL、数据/日志/查询生成、CLI
  web/              Flask 页面与 API
tests/              pytest 测试（35 项）
```

## 边界与简化

- SQL 方言以 MySQL 8.0 为参照做了简化，支持单表/双表 SELECT、常用谓词、IN、
  BETWEEN、LIKE、JOIN、聚合投影、ORDER BY/LIMIT；不支持子查询、窗口函数、DDL 变更执行。
- 转换规则覆盖最常见的类型族组合；优化器只实现嵌套循环连接（可走索引）与等值哈希连接，
  用于真实复现转换对访问路径和耗时的影响，不是完整的成本优化器。
- 字符集转换用 Python 编解码验证无损性，语义上对应 MySQL 的 CONVERT 可行性检查。
