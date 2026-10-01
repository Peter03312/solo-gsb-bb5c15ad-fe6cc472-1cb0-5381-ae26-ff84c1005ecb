# 批次追溯服务（batch-trace）

面向小型食品加工厂的**本地批次追溯服务**：登记原料批次、记录生产投料与产出、
把批次分装为可独立投料的子批次、按**库位**核算与调拨批次库存、对账实不符的
批次库位做**盘点盘亏核销**、查询批次余量
（全局 + 逐库位）与上下游谱系，并在发现疑似污染批次时沿投料与分装关系找出并
隔离全部后代产出。

- 语言/框架：Python 3.12 + FastAPI + SQLAlchemy 2
- 关系数据库：PostgreSQL 16（Docker Compose 一键启动）；无数据库时自动回退本地 SQLite 文件库
- 计量：`NUMERIC(18,4)` 定点数，数量必须为**正数**，同一笔单据内**单位一致**
- 部署：Dockerfile 构建镜像，`docker compose up` 安装依赖、建库建表并启动

---

## 一、快速启动

### 方式 A：Docker Compose（推荐，含 PostgreSQL）

```bash
docker compose up --build -d
```

启动后：

- API 服务地址：`http://localhost:8080`
- 健康检查：<http://localhost:8080/health>
- 交互式接口文档（Swagger UI）：<http://localhost:8080/docs>

API 容器会等待 PostgreSQL 健康后自动**建库建表**（`app/entrypoint.py` 调用 `CREATE TABLE IF NOT EXISTS ...`），
数据持久化在 compose 卷 `trace_pgdata` 中。

停止与清理：

```bash
docker compose down          # 停止，保留数据
docker compose down -v       # 停止并删除数据库卷
```

### 方式 B：本地直接运行（自动使用 SQLite）

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m app.entrypoint
```

不设置 `DATABASE_URL` 时会在项目根目录创建 `trace.db`，服务同样监听 `http://0.0.0.0:8080`。

### 运行测试

```bash
pip install -r requirements-dev.txt
pytest
```

---

## 二、配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | 见下 | SQLAlchemy 数据库连接串。compose 中为 `postgresql+psycopg2://trace:trace@db:5432/trace`；未设置时回退 `sqlite:///./trace.db` |
| `DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` | `db` / `5432` / `trace` / `trace` / `trace` | 未设置 `DATABASE_URL` 时用于拼装 PostgreSQL 连接串 |
| `HOST` | `0.0.0.0` | HTTP 监听地址 |
| `PORT` | `8080` | HTTP 监听端口 |
| `DB_POOL_SIZE` | `5` | PostgreSQL 连接池大小 |

compose 的端口映射：宿主机 `8080` → 容器 `8080`，可在 `docker-compose.yml` 的
`ports` 处修改宿主机端口。

---

## 三、HTTP 接口

所有写操作（`POST`）都必须携带请求头 `Idempotency-Key`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/raw-batches` | 登记一个原料批次及初始数量（进入默认库位） |
| POST | `/productions` | 提交一次生产：多批次投入 → 一个产出批次（投料可指定库位） |
| POST | `/batches/{batch_no}/splits` | 批次分装：一个父批次 → 多个可独立投料的子批次（可指定出库库位） |
| POST | `/transfers` | 库位调拨：同一批次在两个不同库位间移动正数数量 |
| POST | `/stock-checks` | 仓库盘点：凭唯一盘点单号提交多行盘亏核销，按差额扣减库位与全局余量 |
| GET | `/stock-checks/{stock_check_no}` | 查询盘点单状态及逐行原值、差额与单位 |
| GET | `/batches/{batch_no}/locations` | 查询批次逐库位余额（各库位之和等于全局余量） |
| POST | `/productions/{output_no}/cancellation` | 撤销录入错误的生产单：按原消耗库位返还、作废产出与投料边 |
| GET | `/productions/{output_no}` | 按产出编号查询生产原单据（含撤销状态/原因） |
| GET | `/batches/{batch_no}` | 查询批次余量与状态（`remaining_qty` 为全局余量，含义不变） |
| GET | `/batches/{batch_no}/lineage` | 查询上下游谱系（递归，不含已撤销投料边） |
| POST | `/contamination` | 标记疑似污染批次，返回并隔离全部后代 |
| POST | `/reviews` | 质检复核确认误报：撤销疑似标记并重算隔离 |

### 业务规则

1. 数量一律为正数，使用定点数计算；同一生产单内所有批次单位必须一致。
2. 产出量 **不得超过** 投入总量（允许加工损耗，即产出 < 投入）。
3. 批次编号全局唯一、**不得复用**（原料批次与产出批次共用编号空间）。
4. 来源不存在、用量无效（非正数）、单位不一致或余量不足时，**整笔拒绝，不改变任何余量**。
5. 污染规则：标记某批次疑似污染后，其全部直接/间接产出（混料后代）**整体隔离**；
   隔离批次（含疑似批次本身）**不得再投料**，提交生产时整笔拒绝。
6. 复核规则：质检复核确认误报时，`POST /reviews` 提交**当前疑似批次编号**与
   **复核说明**（非空白），撤销其疑似标记并重算该批次及全部后代的隔离状态——
   自身或任一祖先仍疑似则继续隔离，否则解除。混料后代不会因一个来源解除就忽略
   另一疑似来源；编号不存在报 `SOURCE_NOT_FOUND`(404)，批次当前非疑似（从未标记
   或已撤销）报 `BATCH_NOT_SUSPECT`(409)，二者均不改变任何状态。批次可经历
   “标记 → 复核撤销 → 再次标记 → 再次复核”。
7. 幂等：同一 `Idempotency-Key` + 同一请求体重试，返回首次的状态码与响应体、不重复扣减、不重复撤销；
   同一 key + 不同请求体返回 `409 IDEMPOTENCY_CONFLICT`。
8. 并发：生产提交在单事务内对来源批次加行锁（PostgreSQL `SELECT ... FOR UPDATE`，
   按编号排序加锁避免死锁；SQLite 以 `BEGIN IMMEDIATE` 串行化写事务），并发提交**不会超用**同一批次余量。
   污染标记/复核对“自身 + 全部后代”的闭包按编号排序加行锁，与生产事务互斥，
   并发结果等价于复核、污染标记、投料三者的某一串行顺序；隔离批次绝不会被投料。
9. 生产单撤销：录入错误时，操作员可凭**产出批次编号**与**非空撤销原因**
   `POST /productions/{output_no}/cancellation` 撤销该单。**仅当产出同时满足**
   以下条件才允许撤销，任一不符整笔拒绝、**不改变任何库存**：
   - 产出编号存在且确为生产产出（原料批次不可撤销）；
   - 该产出**从未被后续生产投料**（以保留的投料记录严格判定，即便下游单据
     后来也被撤销，本产出仍视为“曾被投料”而不可撤销）；
   - 产出**当前非疑似**（仅因疑似祖先被连带隔离不阻止撤销）；
   - 产出**无质检复核记录**；
   - 产出**从未在仓库盘点中发生盘亏**（仅平账不阻止；见第 13 条，
     `OUTPUT_HAS_STOCK_LOSS` 409）；
   - 该生产单**尚未撤销**（重复撤销明确拒绝）。

   撤销在**同一事务内**：按各来源的**原投料量**返还余量（即使来源批次已隔离
   也照常返还；返还不会超过其历史消耗，余量恒不超过初始量）；生产单标记为
   已撤销并记录原因/时间。产出批次行与原单据**保留可查**（编号不得复用），
   但已撤销产出**不得再投料**，其投料边**不再进入有效谱系查询**、也**不再
   参与疑似隔离传播**；产出若此前仅因疑似祖先被连带隔离，撤销边后随之解除。
   编号不存在报 `SOURCE_NOT_FOUND`(404)，原料批次报 `PRODUCTION_NOT_FOUND`(404)；
   已被投料 `OUTPUT_ALREADY_CONSUMED`(409)、当前疑似 `OUTPUT_IS_SUSPECT`(409)、
   有复核记录 `PRODUCTION_HAS_REVIEW`(409)、重复撤销
   `PRODUCTION_ALREADY_CANCELLED`(409)、撤销产出投料 `BATCH_CANCELLED`(409)。
10. 撤销并发：撤销与污染标记/复核一样先取 QC 串行化锁，再按编号升序锁定
    “产出 + 各来源”，使撤销 / 投料 / 污染标记 / 复核任意并发都等价于某一串行
    顺序，不会超用余量或误放行（例如“撤销成功且该产出又被投料”不会发生）。
11. 批次分装（split）：同一原料或产出批次可按**父批次编号**
    `POST /batches/{batch_no}/splits`，一笔提交**多个互异新编号**及各自**正数
    分装量**，服务返回**父批次新余量、子批次清单与分装关系**。
    - 子批次**继承父批次单位**，其初始/当前余量等于分装量；**分装不产生
      生产单**（子批次查不到生产原单据），但子批次与普通批次一样**可作为
      正常生产来源**投料，也可再次分装；
    - 校验：父批次须**存在**（`SOURCE_NOT_FOUND` 404）、**未隔离**
      （`BATCH_QUARANTINED` 409）且**非已撤销产出**（`BATCH_CANCELLED` 409）；
      同一笔分装内新编号必须**互异**（`DUPLICATE_SPLIT_TARGET` 422）；
      新编号全局**不得复用**（`BATCH_NO_REUSED` 409）；分装总量必须为正且
      **不得超过父批次余量**（`INSUFFICIENT_BALANCE` 409，details 中
      `requested` 为分装总量）。任一不符**整笔拒绝、不改任何库存**，
      已扣减到一半也会随事务回滚；
    - **谱系**：分装边（`kind: "SPLIT"`）与投料边（`kind: "PRODUCTION"`）
      **一起遍历**，祖先/后代、深度与边集合均同时包含两类关系；同一批次经
      多条路径（混料 + 分装形成的菱形 DAG）只出现一次，深度取最短路径；
    - **隔离传播**：祖先被标记疑似或经复核重算后，隔离沿**分装边与投料边
      两类关系**传播并重算；混料后代仍按“任一疑似来源”隔离。分装新建的
      子批次初始不隔离；在隔离父批次上提交分装会被整笔拒绝；
    - **撤销联动**：父批次若为**生产产出**，一旦被分装即**视为曾被使用**
      ——即使子批次尚未投料，原撤销入口也以 `OUTPUT_ALREADY_SPLIT`(409)
      拒绝（details 的 `split_into` 列出子批次编号），且不改变任何库存；
    - **并发**：分装与生产提交共用同一套单事务 + 按编号升序行锁
      （SQLite 为 `BEGIN IMMEDIATE` 串行化），分装 / 投料并发绝不超用；
      与污染标记 / 复核经行锁与 QC 表锁互斥，结果等价于某一串行顺序，
      绝不漏隔离；
    - 幂等沿用既有 `Idempotency-Key` 规则：同键同体重放返回首次结果、
      只扣一次，同键异体 `409 IDEMPOTENCY_CONFLICT`，失败不固化可重试。
12. **库位库存（stock by location）**：批次余量按库位核算。
    - **两层库存**：`GET /batches/{batch_no}` 的 `remaining_qty` 仍是
      **全局余量**（字段与含义均不变）；`GET /batches/{batch_no}/locations`
      返回逐库位余额（按库位编号排序），**各库位之和恒等于全局余量**。
      既有批次余量与此后新登记批次的数量都归入**默认库位**（编号
      `DEFAULT`）；生产产出与分装子批次也始终进入默认库位。
    - **库位调拨** `POST /transfers`：凭**唯一调拨单号**、批次编号、
      **来源库位**、**目标库位**与**正数数量**把库存从来源库位移到目标
      库位，响应返回调拨后**两库位余额**（并附该批次全部库位余额与全局
      余量）。调拨只改变库位分布、**不改变全局余量**；目标库位首次使用时
      自动以零余额创建。以下情形**整单拒绝、两库位余额均不变**：
      来源/目标库位编号为空或纯空白（`422 VALIDATION_ERROR`）、来源与目标
      **相同**（去空白后相同亦算；`422 VALIDATION_ERROR`，服务层兜底码
      `TRANSFER_SAME_LOCATION`）、批次不存在
      （`404 SOURCE_NOT_FOUND`）、来源库位不存在
      （`404 LOCATION_NOT_FOUND`，且不创建目标库位）、来源库位余额不足
      （`409 INSUFFICIENT_BALANCE`，`details.location_code` 指出来源库位）、
      调拨单号已被占用（`409 TRANSFER_NO_REUSED`，单号全局唯一不得复用）。
      **隔离批次（含疑似）与已撤销产出批次允许调拨**——隔离只禁止投料与
      分装。
    - **投料按库位**：`POST /productions` 的每笔投入新增**可选**字段
      `location_code`。**未指定**（省略或为 `null`）时按**库位编号顺序**
      （字典序）跨库位依次扣减，直至扣满；**指定**时**仅扣该库位**，
      该库位不存在报 `404 LOCATION_NOT_FOUND`、该库位余额不足报
      `409 INSUFFICIENT_BALANCE`（即使全局余量充足也整笔拒绝）。任一来源
      不符整笔生产拒绝、不改变任何库位。
    - **分装按库位**：`POST /batches/{batch_no}/splits` 请求体新增可选
      `location_code`，扣减规则与投料一致（未指定按编号顺序、指定仅扣该
      库位）；子批次一律进入默认库位。
    - **撤销按原库位返还**：撤销生产单时，各来源严格按当时的**逐库位实际
      消耗库位与数量**返还（未指定库位的投料按当时跨库位扣减的明细返还），
      而不是统一回默认库位；产出批次随撤销作废，其默认库位行保留可查。
      升级前的历史投料在启动时回填为整笔计入默认库位，撤销历史单据时返还
      默认库位。
    - **并发**：调拨、投料、分装、撤销全部在单事务内遵循统一加锁顺序——
      先按批次编号升序锁批次行，再按（批次、库位编号）锁库位行
      （PostgreSQL `SELECT ... FOR UPDATE`；SQLite 由 `BEGIN IMMEDIATE`
      全库串行）。因此任意并发组合都**不会超用**，也不会使库位层与全局层
      **余额失配**；两个相同调拨单号并发时唯一约束兜底，恰好一笔成功。
    - 幂等沿用既有写操作的 `Idempotency-Key` 重放与冲突语义：同键同体重放
      返回首次结果、库存只移动一次，同键异体 `409 IDEMPOTENCY_CONFLICT`，
      失败不固化可重试。
13. **仓库盘点（stock check）**：发现批次库位账实不符时，凭**唯一盘点单号**
    `POST /stock-checks` 提交**多条互异行**，每行给出批次、库位、**账面量**
    `book_qty` 与**实点量** `actual_qty`。
    - 两种数量均允许为 **0** 但**不得为负**，实点量**不得高于**账面量
      （不允许盘盈；`422 VALIDATION_ERROR`）；同一单内 `(批次, 库位)`
      **必须互异**（`422 DUPLICATE_STOCK_CHECK_LINE`），整单**至少一条
      盘亏**（`422 STOCK_CHECK_NO_LOSS`）。
    - 提交时在单事务内逐行核对**当前库位实存恰等于所填账面量**：批次不存在
      `404 SOURCE_NOT_FOUND`，库位不存在 `404 LOCATION_NOT_FOUND`（不自动
      建行），账面已变动（当前实存 ≠ 申报账面）`409 STOCK_CHECK_BOOK_CHANGED`
      （details 给出 `book_qty` 与 `current_qty`）。任一不符**整单拒绝、
      不留盘点记录、不改任何库存**，同键修正后可重试。
    - 全部核对通过后**同事务**按差额 `book_qty - actual_qty` 扣减对应
      **库位余额**与批次**全局余量**；既有 `remaining_qty` 字段仍表示
      **实存**，各库位之和仍等于全局余量（非负、不超扣由 CHECK 约束与
      行锁共同保证）。
    - 成功单据**只追加、不可修改**：原账面量、实点量、差额、单位（取批次
      单位固化）与提交时间逐行保留，`GET /stock-checks/{单号}` 返回
      `status`（`POSTED`）、`checked_at` 及逐行原值；盘点单号全局唯一、
      **不得复用**（`409 STOCK_CHECK_NO_REUSED`）。
    - **隔离批次（含疑似）与已撤销产出仍可核销**盘亏——盘点是库存校正而非
      投料/分装；盘点**不产生谱系边、不改历史投料与谱系**。
    - **盘亏产出不得撤销**：生产产出一旦在盘点单中发生过盘亏（任一行
      `loss_qty > 0`），撤销原生产单时以
      `OUTPUT_HAS_STOCK_LOSS`(409) 拒绝（details 的 `stock_check_nos`
      列出相关盘点单）；仅平账（差额 0）不阻止撤销，**来源**批次盘亏也
      不影响其下游产出撤销。
    - **并发**：盘点沿用全局统一加锁顺序——先按批次编号升序锁批次行，再按
      （批次, 库位编号）锁库位行（PostgreSQL `SELECT ... FOR UPDATE`；
      SQLite 为 `BEGIN IMMEDIATE` 全库串行）。与投料、分装、调拨、撤销及
      其他盘点交错时，核对与扣减原子完成：后到者要么在锁后读到新余额
      （账面已变动 -> 整单拒绝），要么按新账面核对通过，绝不超扣、绝不
      覆盖新余额；两个相同盘点单号并发时唯一约束兜底，恰好一笔成功。
    - 幂等沿用既有 `Idempotency-Key` 规则：同键同体重放返回首次结果、
      差额只扣一次，同键异体 `409 IDEMPOTENCY_CONFLICT`，失败不固化可重试。

### 错误响应格式

```json
{ "error": { "code": "INSUFFICIENT_BALANCE", "message": "批次 'RM-1' 余量不足：请求 200，剩余 100", "details": { } } }
```

常见错误码：`SOURCE_NOT_FOUND`(404)、`PRODUCTION_NOT_FOUND`(404)、`BATCH_NO_REUSED`(409)、
`INSUFFICIENT_BALANCE`(409)、`UNIT_MISMATCH`(422)、`OUTPUT_EXCEEDS_INPUT`(422)、
`DUPLICATE_INPUT`(422)、`DUPLICATE_SPLIT_TARGET`(422)、
`VALIDATION_ERROR`(422)、`BATCH_QUARANTINED`(409)、`BATCH_NOT_SUSPECT`(409)、
`OUTPUT_ALREADY_CONSUMED`(409)、`OUTPUT_ALREADY_SPLIT`(409)、`OUTPUT_IS_SUSPECT`(409)、
`PRODUCTION_HAS_REVIEW`(409)、`PRODUCTION_ALREADY_CANCELLED`(409)、`BATCH_CANCELLED`(409)、
`IDEMPOTENCY_CONFLICT`(409)、`LOCATION_NOT_FOUND`(404)、
`TRANSFER_SAME_LOCATION`(422)、`TRANSFER_NO_REUSED`(409)、
`DUPLICATE_STOCK_CHECK_LINE`(422)、`STOCK_CHECK_NO_LOSS`(422)、
`STOCK_CHECK_NO_REUSED`(409)、`STOCK_CHECK_BOOK_CHANGED`(409)、
`STOCK_CHECK_NOT_FOUND`(404)、`OUTPUT_HAS_STOCK_LOSS`(409)。

---

## 四、HTTP 调用样例

### 1. 登记原料批次

```bash
curl -X POST http://localhost:8080/raw-batches \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: raw-rm-a-001' \
  -d '{"batch_no":"RM-A","unit":"kg","quantity":"100","note":"高筋面粉"}'
```

```json
{"batch_no":"RM-A","unit":"kg","initial_qty":100.0,"remaining_qty":100.0,
 "is_suspect":false,"is_quarantined":false,"note":"高筋面粉"}
```

### 2. 提交一次混料生产（多批次投入 → 一个产出）

```bash
curl -X POST http://localhost:8080/productions \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: prod-mix1-001' \
  -d '{
        "output_batch_no": "MIX-1",
        "unit": "kg",
        "output_quantity": 90,
        "inputs": [
          {"batch_no": "RM-A", "quantity": 40},
          {"batch_no": "RM-B", "quantity": 55}
        ]
      }'
```

### 3. 安全重试（同一 key + 同一内容）

```bash
# 与上一条完全相同的请求：返回原结果，余量只扣一次
curl -X POST http://localhost:8080/productions \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: prod-mix1-001' \
  -d '{ ...同上... }'

# 同一 key 换不同内容：
# 409 {"error":{"code":"IDEMPOTENCY_CONFLICT", ...}}
```

### 4. 查询批次余量

```bash
curl http://localhost:8080/batches/RM-A
# {"batch_no":"RM-A","unit":"kg","initial_qty":100.0,"remaining_qty":60.0, ...}
```

### 5. 查询上下游谱系

```bash
curl http://localhost:8080/batches/RM-A/lineage
```

```json
{
  "batch_no": "RM-A",
  "upstream": [],
  "downstream": [
    {"batch_no": "MIX-1", "depth": 1, "remaining_qty": 40.0,
     "unit": "kg", "is_suspect": false, "is_quarantined": false},
    {"batch_no": "FG-1",  "depth": 2, "remaining_qty": 50.0,
     "unit": "kg", "is_suspect": false, "is_quarantined": false}
  ],
  "edges": [
    {"from": "RM-A", "to": "MIX-1", "kind": "PRODUCTION"},
    {"from": "MIX-1", "to": "FG-1", "kind": "PRODUCTION"}
  ]
}
```

`depth` 表示与查询批次相隔的关系层数；`edges` 给出当前谱系子图中的有向边，
`kind` 为 `PRODUCTION`（投料边：来源 → 产出）或 `SPLIT`（分装边：父批次 →
子批次），两类关系在谱系与隔离传播中等价遍历。同一批次经多条路径到达时
（混料/分装形成的菱形 DAG）只出现一次，`depth` 取最短路径。

### 6. 标记疑似污染并找出全部后代

```bash
curl -X POST http://localhost:8080/contamination \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: cont-20260930-01' \
  -d '{"batch_no":"RM-A","note":"抽检农残超标"}'
```

```json
{
  "source": "RM-A",
  "is_suspect": true,
  "affected_batch_nos": ["MIX-1", "FG-1"],
  "newly_quarantined": ["MIX-1", "FG-1"]
}
```

`MIX-1`（混料时混入 RM-A）与 `FG-1`（间接后代）被**整体隔离**；与 RM-A 无关的批次不受影响。
此后再用隔离批次投料会被拒绝：

```bash
curl -X POST http://localhost:8080/productions \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: prod-fg2-001' \
  -d '{"output_batch_no":"FG-2","unit":"kg","output_quantity":5,
       "inputs":[{"batch_no":"MIX-1","quantity":5}]}'
# 409 {"error":{"code":"BATCH_QUARANTINED","message":"批次 'MIX-1' 已隔离，隔离批次不得再投料"}}
```

### 7. 质检复核：确认误报，撤销疑似标记并重算隔离

沿用上例：`RM-A` 已被标记疑似，混料后代 `MIX-1 = RM-A + RM-B`、`MIX-1 -> FG-1`
均被隔离。质检复检确认 RM-A 为误报：

```bash
curl -X POST http://localhost:8080/reviews \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: review-rm-a-20260930-01' \
  -d '{"batch_no":"RM-A","review_note":"留样复测未检出农残，确认误报"}'
```

```json
{
  "reviewed_batch_no": "RM-A",
  "is_suspect": false,
  "review_note": "留样复测未检出农残，确认误报",
  "affected_batch_nos": ["MIX-1", "FG-1"],
  "released_batch_nos": ["FG-1", "MIX-1", "RM-A"],
  "still_quarantined": []
}
```

- `affected_batch_nos`：本次重算涉及的全部后代；
- `released_batch_nos`：本次**解除隔离**的批次（复核批次自身 + 不再有任何疑似
  祖先的后代）；`still_quarantined`：重算范围内仍须隔离的批次。
- 此后 `GET /batches/{no}` 与 `/batches/{no}/lineage` 同步反映 `is_suspect=false`、
  `is_quarantined=false`；解除隔离的批次可重新投料。

混料来源不会被“连带误放”：若 RM-B 当时也处于疑似状态，撤销 RM-A 只会放行
RM-A 自身，`MIX-1`/`FG-1` 因仍有疑似祖先 RM-B 而继续隔离：

```json
{ "released_batch_nos": ["RM-A"], "still_quarantined": ["FG-1", "MIX-1"] }
```

非法复核明确报错且不改变状态：

```bash
# 编号不存在
curl -i -X POST http://localhost:8080/reviews \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: rv-x' \
  -d '{"batch_no":"GHOST","review_note":"x"}'
# 404 SOURCE_NOT_FOUND

# 已撤销 / 从未疑似
curl -i -X POST http://localhost:8080/reviews \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: rv-a2' \
  -d '{"batch_no":"RM-A","review_note":"再次撤销"}'
# 409 BATCH_NOT_SUSPECT
```

### 8. 整笔拒绝示例（余量不足时不产生副作用）

```bash
# RM-A 当前余量 60，却请求消耗 200
curl -i -X POST http://localhost:8080/productions \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: prod-bad-001' \
  -d '{"output_batch_no":"MIX-X","unit":"kg","output_quantity":200,
       "inputs":[{"batch_no":"RM-A","quantity":200}]}'
# HTTP/1.1 409 ... INSUFFICIENT_BALANCE；MIX-X 未创建，RM-A 余量仍为 60
```

### 9. 撤销录入错误的生产单（返还来源投料、作废产出）

接上例，一次混料生产 `MIX-1 = 40 RM-A + 20 RM-B`（产出 55）录入有误，
操作员凭产出编号与非空撤销原因撤销：

```bash
curl -X POST http://localhost:8080/productions/MIX-1/cancellation \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: cancel-mix1-20260930-01' \
  -d '{"cancel_reason":"产出编号录入错误，实际应为 MIX-1B"}'
```

```json
{
  "output_batch_no": "MIX-1",
  "status": "CANCELLED",
  "cancel_reason": "产出编号录入错误，实际应为 MIX-1B",
  "cancelled_at": "2026-09-30T06:20:31.123456+00:00",
  "returned_inputs": [
    {"batch_no": "RM-A", "returned_quantity": 40.0},
    {"batch_no": "RM-B", "returned_quantity": 20.0}
  ]
}
```

撤销结果含**产出编号、返还明细（各来源原投料量）与撤销状态**；同一事务内
`RM-A` 余量回到 100、`RM-B` 回到 50（即使某来源当时已隔离也照常返还）。

撤销后保留产出编号、原单据与撤销原因供查询：

```bash
# 批次查询带 is_cancelled=true（编号仍被占用，不得复用）
curl http://localhost:8080/batches/MIX-1
# {"batch_no":"MIX-1", ..., "remaining_qty":55.0, "is_cancelled":true}

# 原单据（含撤销状态/原因/时间）
curl http://localhost:8080/productions/MIX-1
# {"output_batch_no":"MIX-1", ..., "is_cancelled":true,
#  "cancel_reason":"产出编号录入错误，实际应为 MIX-1B", "cancelled_at":"..."}

# 谱系不再包含该投料边
curl http://localhost:8080/batches/RM-A/lineage
# {"batch_no":"RM-A","is_cancelled":false,"upstream":[],"downstream":[],"edges":[]}
```

已撤销产出不得再投料（编号也不得复用）；撤销的投料边不再进入疑似传播：

```bash
curl -i -X POST http://localhost:8080/productions \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: prod-after-cancel' \
  -d '{"output_batch_no":"FG-9","unit":"kg","output_quantity":5,
       "inputs":[{"batch_no":"MIX-1","quantity":5}]}'
# 409 BATCH_CANCELLED：批次 'MIX-1' 为已撤销生产单的产出，已撤销产出不得再投料
```

撤销沿用现有写请求的幂等约定：同 `Idempotency-Key` + 同请求体重放返回首次
结果且只返还一次；同键异体返回 `409 IDEMPOTENCY_CONFLICT`。

条件不符明确拒绝且不改库存（示例）：

```bash
# 编号不存在
curl -i -X POST http://localhost:8080/productions/GHOST/cancellation \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: c-x' \
  -d '{"cancel_reason":"x"}'
# 404 SOURCE_NOT_FOUND

# 产出编号实为原料批次（无生产单）
# 404 PRODUCTION_NOT_FOUND

# 产出已被后续生产投料（即便下游单据后来撤销，也仍按“曾被投料”拒绝）
# 409 OUTPUT_ALREADY_CONSUMED，details.consumed_by 列出投料它的下游产出

# 产出当前疑似（须先复核；但复核会留下记录而同样不可撤销）
# 409 OUTPUT_IS_SUSPECT / 409 PRODUCTION_HAS_REVIEW

# 重复撤销
# 409 PRODUCTION_ALREADY_CANCELLED

# 撤销原因为空或纯空白
# 422 VALIDATION_ERROR
```

### 10. 分装：把同一批次拆成可独立投料的子批次

把原料批次 `RM-1`（100 kg）一次性分装为两个子批次 `RM-1-A`（30 kg）与
`RM-1-B`（20 kg）：

```bash
curl -X POST http://localhost:8080/batches/RM-1/splits \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: split-rm1-20261001-01' \
  -d '{"splits":[
        {"batch_no":"RM-1-A","quantity":"30"},
        {"batch_no":"RM-1-B","quantity":"20"}
      ]}'
```

```json
{
  "parent_batch_no": "RM-1",
  "parent_remaining_qty": 40.0,
  "splits": [
    {"batch_no":"RM-1-A","unit":"kg","quantity":30.0,"remaining_qty":30.0,
     "is_suspect":false,"is_quarantined":false},
    {"batch_no":"RM-1-B","unit":"kg","quantity":20.0,"remaining_qty":20.0,
     "is_suspect":false,"is_quarantined":false}
  ],
  "relations": [
    {"parent_batch_no":"RM-1","child_batch_no":"RM-1-A","quantity":30.0},
    {"parent_batch_no":"RM-1","child_batch_no":"RM-1-B","quantity":20.0}
  ]
}
```

子批次继承单位、**分装不产生生产单**，但可作为正常生产来源投料，也可再次分装：

```bash
# 子批次作为正常来源混料投料
curl -X POST http://localhost:8080/productions \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: prod-mix2-001' \
  -d '{"output_batch_no":"MIX-2","unit":"kg","output_quantity":25,
       "inputs":[{"batch_no":"RM-1-A","quantity":20},{"batch_no":"RM-1-B","quantity":5}]}'

# 子批次继续分装
curl -X POST http://localhost:8080/batches/RM-1-A/splits \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: split-rm1a-001' \
  -d '{"splits":[{"batch_no":"RM-1-A-1","quantity":"10"}]}'
```

分装边与投料边一起进入谱系（`kind` 区分两类），疑似隔离沿两类边传播：

```bash
curl -X POST http://localhost:8080/contamination \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: cont-rm1-01' \
  -d '{"batch_no":"RM-1","note":"抽检农残超标"}'
# affected_batch_nos 同时包含分装后代（RM-1-A/RM-1-B/RM-1-A-1）与
# 投料后代（MIX-2）；混料后代仍按“任一疑似来源”整体隔离
```

任一不符整笔拒绝、不改库存（示例）：

```bash
# 父批次不存在：404 SOURCE_NOT_FOUND
# 同一笔分装新编号重复：422 DUPLICATE_SPLIT_TARGET
# 新编号已被占用（原料/产出/子批次共用编号空间）：409 BATCH_NO_REUSED
# 分装总量超过父余量（如余量 40 却分装 30+20）：
#   409 INSUFFICIENT_BALANCE，details.requested 为分装总量 50
# 父批次已隔离（含疑似）：409 BATCH_QUARANTINED
# 父批次为已撤销产出：409 BATCH_CANCELLED
# 分装量为 0/负数或 splits 为空：422 VALIDATION_ERROR
```

父批次若是生产产出，分装后视为曾被使用——即使子批次尚未投料，撤销入口也拒绝：

```bash
# MIX-1 是生产产出，曾分装出 MIX-1-A（即便 MIX-1-A 从未投料）
curl -i -X POST http://localhost:8080/productions/MIX-1/cancellation \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: cancel-mix1-01' \
  -d '{"cancel_reason":"录入错误"}'
# HTTP/1.1 409
# {"error":{"code":"OUTPUT_ALREADY_SPLIT",
#   "details":{"batch_no":"MIX-1","split_into":["MIX-1-A"]}, ...}}
# 来源余量不返还、生产单不撤销
```

### 11. 库位库存与调拨

批次按库位核算库存：登记/产出/分装产生的数量都进入默认库位 `DEFAULT`，
批次查询的 `remaining_qty` 仍是全局余量（含义不变），逐库位余额单独查询：

```bash
curl -X POST http://localhost:8080/raw-batches \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: raw-loc-001' \
  -d '{"batch_no":"RM-1","unit":"kg","quantity":"100"}'

curl http://localhost:8080/batches/RM-1/locations
```

```json
{
  "batch_no": "RM-1",
  "unit": "kg",
  "remaining_qty": 100.0,
  "locations": [{"location_code": "DEFAULT", "quantity": 100.0}]
}
```

凭唯一调拨单号把数量在两个不同库位间移动，响应返回两库位余额，全局余量不变：

```bash
curl -X POST http://localhost:8080/transfers \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: tr-0001' \
  -d '{
        "transfer_no": "TR-20261001-01",
        "batch_no": "RM-1",
        "source_location": "DEFAULT",
        "target_location": "A-01",
        "quantity": 30
      }'
```

```json
{
  "transfer_no": "TR-20261001-01",
  "batch_no": "RM-1",
  "source_location": "DEFAULT",
  "target_location": "A-01",
  "quantity": 30.0,
  "source_balance": 70.0,
  "target_balance": 30.0,
  "remaining_qty": 100.0,
  "locations": [
    {"location_code": "A-01", "quantity": 30.0},
    {"location_code": "DEFAULT", "quantity": 70.0}
  ]
}
```

目标库位首次使用时自动创建；整单拒绝的情形（库位为空/相同、批次不存在、
来源库位不存在或不足、单号重复）均不改变任何库位余额：

```bash
# 来源与目标相同（含去空白后相同，如 " A " 与 "A"）
# 422 {"error":{"code":"VALIDATION_ERROR", ...}}

# 来源库位不存在（不会创建目标库位）
curl -i -X POST http://localhost:8080/transfers \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: tr-x' \
  -d '{"transfer_no":"TR-X","batch_no":"RM-1",
       "source_location":"NOPE","target_location":"B-02","quantity":1}'
# 404 {"error":{"code":"LOCATION_NOT_FOUND",
#   "details":{"batch_no":"RM-1","location_code":"NOPE"}}}

# 来源库位不足（即使全局余量充足）
# 409 {"error":{"code":"INSUFFICIENT_BALANCE",
#   "details":{"location_code":"A-01","requested":"40","remaining":"30.0000"}}}

# 调拨单号重复
# 409 {"error":{"code":"TRANSFER_NO_REUSED", ...}}
```

投料与分装可指定库位：**未指定**时按库位编号顺序（字典序）跨库位扣减，
**指定**时仅扣该库位：

```bash
# 指定库位投料：仅扣 A-01
curl -X POST http://localhost:8080/productions \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: prod-loc-001' \
  -d '{"output_batch_no":"MIX-L1","unit":"kg","output_quantity":20,
       "inputs":[{"batch_no":"RM-1","quantity":20,"location_code":"A-01"}]}'

# 未指定库位：按库位编号顺序跨库位扣减（A-01 余量优先于 DEFAULT）
curl -X POST http://localhost:8080/productions \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: prod-loc-002' \
  -d '{"output_batch_no":"MIX-L2","unit":"kg","output_quantity":30,
       "inputs":[{"batch_no":"RM-1","quantity":30}]}'

# 分装也可指定出库库位；子批次进入默认库位
curl -X POST http://localhost:8080/batches/RM-1/splits \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: split-loc-001' \
  -d '{"location_code":"DEFAULT",
       "splits":[{"batch_no":"RM-1-P","quantity":10}]}'

curl http://localhost:8080/batches/RM-1/locations
# 各库位 quantity 之和始终等于 remaining_qty（全局余量）
```

撤销生产单时按**原消耗库位**返还：上例 `MIX-L2` 未指定库位、跨 A-01 与
DEFAULT 扣减，撤销后两库位各自收回当时被扣的数量，而不是统一回默认库位：

```bash
curl -X POST http://localhost:8080/productions/MIX-L2/cancellation \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: cancel-loc-001' \
  -d '{"cancel_reason":"投料库位核算有误，撤回"}'
# returned_inputs 仍按来源批次列出总返还量；
# GET /batches/RM-1/locations 可见 A-01 与 DEFAULT 分别收回原消耗数量
```

隔离批次（含疑似）仍可调拨，只是不得投料或分装：

```bash
curl -X POST http://localhost:8080/contamination \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: cont-loc-001' \
  -d '{"batch_no":"RM-1","note":"疑似外包装污染"}'

# 调拨：201 成功（库存可在隔离状态下移位/召回归集）
curl -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/transfers \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: tr-q' \
  -d '{"transfer_no":"TR-Q","batch_no":"RM-1",
       "source_location":"A-01","target_location":"Q-QUARANTINE","quantity":1}'
# 201

# 投料 / 分装：仍 409 BATCH_QUARANTINED
```

### 12. 仓库盘点：多行盘亏核销

月末盘点发现 `RM-1` 在两个库位上均有盘亏，另一批次 `RM-2` 账实相符（平账
行可与盘亏行同单提交，但整单至少一条盘亏）。凭唯一盘点单号一次提交：

```bash
curl -X POST http://localhost:8080/stock-checks \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: sc-20261001-01' \
  -d '{
        "stock_check_no": "SC-20261001-01",
        "lines": [
          {"batch_no":"RM-1","location_code":"DEFAULT","book_qty":70,"actual_qty":60},
          {"batch_no":"RM-1","location_code":"A-01","book_qty":30,"actual_qty":28},
          {"batch_no":"RM-2","location_code":"DEFAULT","book_qty":50,"actual_qty":50}
        ]
      }'
```

```json
{
  "stock_check_no": "SC-20261001-01",
  "status": "POSTED",
  "checked_at": "2026-10-01T09:40:00+00:00",
  "lines": [
    {"batch_no":"RM-1","location_code":"DEFAULT","book_qty":70.0,"actual_qty":60.0,"loss_qty":10.0,"unit":"kg"},
    {"batch_no":"RM-1","location_code":"A-01","book_qty":30.0,"actual_qty":28.0,"loss_qty":2.0,"unit":"kg"},
    {"batch_no":"RM-2","location_code":"DEFAULT","book_qty":50.0,"actual_qty":50.0,"loss_qty":0.0,"unit":"kg"}
  ]
}
```

成功后同事务按差额扣减：`RM-1` 全局余量与两库位各减 10 / 2（各库位之和
仍等于全局余量），`RM-2` 不变。盘点单只追加、原值不可修改，可随时查询：

```bash
curl http://localhost:8080/stock-checks/SC-20261001-01
```

整单拒绝示例（任一不符均不留记录、不改库存，同键可修正后重试）：

```bash
# 账面已变动：此刻 DEFAULT 实存已为 60，却申报账面 70（如并发盘点/调拨已改变库存）
# 409 {"error":{"code":"STOCK_CHECK_BOOK_CHANGED",
#   "details":{"batch_no":"RM-1","location_code":"DEFAULT",
#              "book_qty":"70","current_qty":"60.0000"}}}

# 批次不存在：404 SOURCE_NOT_FOUND
# 库位不存在：404 LOCATION_NOT_FOUND（不会自动建行）
# 盘点单号重复：409 STOCK_CHECK_NO_REUSED（单号不得复用）
# 同单 (批次,库位) 重复：422 DUPLICATE_STOCK_CHECK_LINE
# 整单无一条盘亏：422 STOCK_CHECK_NO_LOSS
# 实点量高于账面量 / 数量为负 / 空行：422 VALIDATION_ERROR
# 查询不存在的单号：404 STOCK_CHECK_NOT_FOUND
```

隔离批次（含疑似）与**已撤销产出**仍可核销盘亏；但一旦某生产**产出**自身
在盘点中发生盘亏，其原生产单不得再撤销：

```bash
# FG-1 曾盘亏 5
curl -i -X POST http://localhost:8080/productions/FG-1/cancellation \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: cancel-after-sc' \
  -d '{"cancel_reason":"录入错误"}'
# 409 {"error":{"code":"OUTPUT_HAS_STOCK_LOSS",
#   "details":{"batch_no":"FG-1","stock_check_nos":["SC-20261001-02"]}}}
```

安全重试沿用统一语义：同一 `Idempotency-Key` + 同一请求体重放返回首次结果、
差额只扣一次；同键异体返回 `409 IDEMPOTENCY_CONFLICT`。

---

## 五、项目结构

```
app/
  config.py       # 环境变量配置
  db.py           # 引擎/会话、SQLite 写锁与 PG 行锁支撑、建表、增量补列与
                  # 库位层回填（既有批次/历史投料归入默认库位）
  models.py       # batches / batch_locations(库位余额) / productions(含撤销字段) /
                  # production_inputs(含申报库位) / production_input_locations
                  # (逐库位消耗明细) / stock_transfers(调拨单) /
                  # stock_checks + stock_check_lines(盘点单与逐行原值快照) /
                  # batch_splits / batch_reviews / idempotency_keys
  schemas.py      # Pydantic 请求响应模型（正数/非负定点数、库位编号、调拨、
                  # 非空撤销原因、分装校验、盘点校验）
  errors.py       # 业务错误码（含 LOCATION_NOT_FOUND / TRANSFER_* / STOCK_CHECK_*）
  service.py      # 核心逻辑：登记、库位调拨、按库位投料/分装、生产事务、
                  # 按原消耗库位撤销返还、仓库盘点核销、幂等、递归谱系、
                  # 污染传播、复核重算
  routes.py       # HTTP 路由（含 /transfers、/stock-checks 与 /batches/{no}/locations）
  main.py         # FastAPI 应用与异常处理
  entrypoint.py   # 容器入口：等待数据库 → 建表 → 启动
tests/            # 端到端 + 并发测试（含盘点功能与盘点并发）
Dockerfile
docker-compose.yml
```

## 六、并发与一致性说明

- 每次生产提交是一个数据库事务：先按编号升序逐个锁定全部来源批次
  （全局统一加锁顺序，杜绝生产事务间死锁），再依次校验单位、隔离状态、
  余量与产出总量，全部通过后才扣减并写入产出；任一校验失败立即回滚。
- PostgreSQL 依赖 `SELECT ... FOR UPDATE` 行锁串行化同一批次的扣减；
  SQLite 写事务以 `BEGIN IMMEDIATE` 开始并设置 `busy_timeout`，写操作全局串行。
- 污染标记与复核是低频 QC 操作：事务一开始先取 QC 串行化锁
  （PostgreSQL 为 `LOCK TABLE batches IN SHARE ROW EXCLUSIVE`，与生产事务的
  行锁/写入互斥；SQLite 由 `BEGIN IMMEDIATE` 全库写锁覆盖），随后读取
  “自身 + 全部后代”闭包并写入 `is_suspect` / `is_quarantined`。因此
  - 复核 / 污染标记 / 投料任意并发，结果都等价于三者的某一串行顺序；
  - 隔离批次不可能被投料（拿不到与 QC 事务的串行化位置，校验到的
    隔离标志与最终谱系一致）；
  - 复核重算隔离状态时以“自身疑似或任一祖先疑似”为谓词，混料后代只要还有
    一个疑似来源就继续隔离。
- 生产单撤销与污染标记/复核同属低频维护事务：先取同一把 QC 串行化锁，再按
  编号升序锁定“产出 + 各来源”，随后做“曾被投料 / 当前疑似 / 有无复核 /
  是否已撤销”全部校验，通过后在同一事务内返还各来源原投料量、置撤销标记、
  令投料边失效并重算产出隔离位。因此撤销 / 投料 / 污染标记 / 复核任意并发，
  结果都等价于某一串行顺序：不会“既撤销又投料”、不会超用余量，也不会误放行；
  两个相同撤销并发时恰好一个成功、另一个以 `PRODUCTION_ALREADY_CANCELLED` 拒绝，
  来源余量只返还一次。
- 幂等记录与业务数据在同一事务内落库，唯一约束 `(method, path, key)` 兜底并发首请求竞争。
- 分装与生产提交同属余量扣减事务：单事务内按编号升序对父批次加行锁
  （SQLite 为 `BEGIN IMMEDIATE` 全库写锁），扣减、建子批次、写分装关系原子
  完成；并发分装 / 并发分装与投料绝不超用，与 QC 事务（标记/复核/撤销）
  经行锁与表锁互斥，结果等价于某一串行顺序，绝不漏隔离。生产产出一旦存在
  分装边，撤销校验即以 `OUTPUT_ALREADY_SPLIT` 拒绝，杜绝“撤销成功且分装仍
  在谱系中传播”的矛盾结局。
- **库位层并发**：所有涉及库位的写事务（调拨、按库位投料/分装、撤销返还）
  遵循统一两级加锁顺序——先按批次编号升序锁批次行，再在每个批次内按库位
  编号升序锁库位行（PostgreSQL `SELECT ... FOR UPDATE`；SQLite 为
  `BEGIN IMMEDIATE` 全库串行）。因此调拨 / 投料 / 分装 / 撤销任意并发都
  不会形成锁环、不会超用任何库位，也不会使“库位余额之和”与“批次全局
  余量”失配；目标库位首次使用时以 `ON CONFLICT DO NOTHING` 幂等建行再
  加锁。同批次反向并发调拨、调拨与指定库位投料竞争同一库位、撤销返还与
  调拨并发等场景，结果都等价于某一串行顺序。隔离状态的判定只依赖批次行，
  与 QC 表锁/行锁的既有互斥关系不变，故隔离批次“可调拨、不可投料/分装”
  在任意并发结局下都成立。
- **盘点并发**：盘点与调拨/投料/分装同属库位扣减事务，沿用同一两级加锁
  顺序（先批次编号升序、再库位编号升序；SQLite `BEGIN IMMEDIATE` 全库
  串行）。单事务内“先逐行核对账面、全部相符后再扣减”，因此与投料、分装、
  调拨、生产撤销及其他盘点任意交错时：后到者在锁后读到的要么是新余额
  （与申报账面不符，`STOCK_CHECK_BOOK_CHANGED` 整单拒绝），要么是申报账面
  所对应的同一状态（扣减生效），绝不超扣任何库位/批次，也绝不覆盖并发
  事务写入的新余额；盘点单头的唯一约束保证相同单号并发时恰好一笔成功。
  盘点与撤销交错时：盘点先落库的产出撤销被 `OUTPUT_HAS_STOCK_LOSS` 拒绝，
  撤销先落库时已撤销产出仍可核销（撤销不扣产出自身余额），结局等价于
  某一串行顺序，两层余额恒等。

> 注：新表 `batch_splits`、`batch_reviews` 与 `productions` 表的撤销字段
> （`is_cancelled` / `cancel_reason` / `cancelled_at`）随启动/入口的
> `CREATE TABLE IF NOT EXISTS` 与幂等 `ALTER TABLE ... ADD COLUMN` 自动
> 创建/补齐，既有数据库升级无需手工迁移；现有登记、生产、污染标记、复核、
> 批次/谱系查询、撤销接口的请求、响应字段与启动方式均保持兼容——
> 谱系响应的 `edges` 仅**新增** `kind` 字段（既有投料边为 `"PRODUCTION"`），
> 批次/谱系响应仍**新增** `is_cancelled` 字段，未撤销/未分装数据行为不变。
>
> 库位层（本轮新增）同样自动升级：新表 `batch_locations`（批次×库位余额）、
> `production_input_locations`（投料逐库位消耗明细，撤销据此返还）、
> `stock_transfers`（调拨单）由启动 `CREATE TABLE IF NOT EXISTS` 创建；
> `production_inputs` 新增可空列 `location_code`（投料时申报的库位，未指定
> 为 `NULL`）。升级时一次性回填：每个尚缺库位行的既有批次以其当前全局余量
> 在默认库位 `DEFAULT` 建行，每笔历史投料的消耗整笔记入默认库位——回填后
> 两层余额恒等，既有批次/生产/撤销接口的字段与行为不变，投料与分装仅
> **新增可选**请求字段 `location_code`，批次查询的 `remaining_qty` 仍是
> 全局余量。
>
> 盘点（本轮新增）为纯增量升级：新表 `stock_checks`（盘点单头，单号唯一）
> 与 `stock_check_lines`（逐行原账面量/实点量/差额/单位快照，含
> `actual_qty <= book_qty`、非负与差额一致等 CHECK 约束）由启动
> `CREATE TABLE IF NOT EXISTS` 自动创建，不改动既有表与列；新接口为
> 纯新增路由，既有全部接口的请求、响应字段、错误码与启动方式保持不变，
> 唯一的既有行为变化是撤销生产单新增一道校验——产出自身曾在盘点中盘亏
> 时以 `OUTPUT_HAS_STOCK_LOSS`(409) 拒绝（此前不存在盘点记录，故既有
> 数据不受影响）。
