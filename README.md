# 异步优化求解服务（FastAPI）

一个本地可跑的异步资源分配优化服务：用户提交算例后拿到任务标识，后台异步完成
贪心求解，可随时查询进度/结果/失败原因，支持排队、取消、幂等重提、崩溃恢复。
算例数据为本地生成的假数据，不依赖任何外部服务。

## 启动（沿用项目原有方式）

```bash
uv sync
uv run python main.py
# 或：uv run uvicorn main:app --reload
```

默认监听 `http://127.0.0.1:8000`，启动后访问：

- Swagger 文档：http://127.0.0.1:8000/docs
- 健康检查：`GET /health`

可通过环境变量调整（均有默认值）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | 监听地址 |
| `OPT_MAX_CONCURRENCY` | `2` | 同时运行的求解任务上限 |
| `OPT_STATE_FILE` | `data/tasks.json` | 本地任务快照文件（原子写） |
| `LOG_LEVEL` | `INFO` | 日志级别 |

## API 一览

| 方法 & 路径 | 说明 |
| --- | --- |
| `GET /health` | 服务与队列水位（running/queued/上限） |
| `POST /api/demo/problem?seed=&n_items=&n_resources=&step_delay_ms=` | 生成本地假算例 |
| `POST /api/tasks` | 提交算例（请求体可带 `idempotency_key`），返回任务标识 |
| `GET /api/tasks/{id}` | 查询任务状态、进度、结果或失败原因 |
| `GET /api/tasks/{id}/result` | 仅在成功时返回结果，其余情况给明确错误码 |
| `POST /api/tasks/{id}/cancel` | 请求取消排队中/运行中的任务 |

提交体示例：

```json
{
  "idempotency_key": "optional-client-unique-key",
  "problem": {
    "items": [
      {"id": "i0", "demand": 2.0, "value": 10.0}
    ],
    "resources": [
      {"id": "r0", "capacity": 8.0}
    ],
    "seed": 42,
    "step_delay_ms": 20
  }
}
```

`step_delay_ms` 给求解的每个迭代注入延时，仅用于本地演示排队/取消，生产传 0。

## 任务状态机（单向推进）

```
QUEUED ──► RUNNING ──► SUCCEEDED
   │          │
   │          ├────────► FAILED        (invalid_input / infeasible / no_solution / internal)
   │          └────────► CANCELLED
   └───────────────────► CANCELLED
```

- `SUCCEEDED` / `FAILED` / `CANCELLED` 是**终态**，状态机不允许任何回退；
- 所有流转集中在 `TaskStore._transition` 中按白名单校验，非法流转直接抛错；
- `QUEUED → CANCELLED` 在调度前立即生效；`RUNNING → CANCELLED` 由求解器在
  最近的迭代检查点检测取消信号后完成（取消期间接口会先看到仍在 running）。

### 对终态的重复操作：行为明确、原因可区分

| 场景 | 行为 |
| --- | --- |
| 成功任务取结果 | `200` 返回结果；重复取结果幂等且内容一致 |
| 排队/运行中取结果 | `409 result_not_ready` |
| 失败任务取结果 | `409 result_unavailable_failed`，错误体内含 `failure_reason` 与 message |
| 取消后取结果 | `409 result_unavailable_cancelled` |
| 对排队/运行任务取消 | `200`，排队立即 cancelled；运行中送达取消信号 |
| 对已取消任务再取消 | `200`，幂等，仍为 cancelled |
| 对成功任务取消 | `409 task_already_succeeded` |
| 对失败任务取消 | `409 task_already_failed` |
| 任务标识不存在 | `404 task_not_found` |

### 失败原因（`failure_reason`）不混用

- `invalid_input`（HTTP 400，任务不会创建）：空 items/resources、重复 id、
  非正数、NaN/Inf、负的 `step_delay_ms` 等；结构/类型错误由 Pydantic 返回 422。
- `infeasible`（任务创建后失败）：可证明不可行 —— 单件需求超过最大资源容量，
  或总需求超过总容量。
- `no_solution`：总容量充足、理论上可能可行，但本地贪心启发式未能放置全部物品。
- `cancelled`：任务被取消（此时无结果）。
- `internal`：未预期异常的兜底。

## 并发与队列策略

- 后台使用单调度线程 + 每任务一个工作线程；处于 `RUNNING` 的任务数严格不超过
  `OPT_MAX_CONCURRENCY`；
- 超出上限的任务保留在 `QUEUED`，**不失败、不丢弃**，任一任务结束后按提交顺序
  自动拉起；
- 排队任务被取消后直接进入 `CANCELLED`，永远不会被调度，因此**取消后不会再
  产生结果**；运行中任务以协作式取消（每个迭代检查信号）保证同样不变量；
- 任务快照在状态变化时原子写入本地 JSON（临时文件 + `os.replace`），进度每
  推进 10% 落一次盘以降低 IO。

### 进程重启 / 崩溃恢复

启动时读取快照，**所有非终态（queued/running）任务一律重置为 `QUEUED`、
进度归零并重新排队**，因此被 `kill -9` 留下的“僵尸 running”不会永久卡住，
会被正常调度直至终态；已成功/失败/取消的任务原样恢复，结果与幂等索引保留。

## 幂等规则

- 请求体顶层的 `idempotency_key` 为可选键；不带键的提交每次都创建新任务；
- 服务端对 `problem` 做规范化 JSON（排序键、无空格）后取 SHA-256 作为负载指纹；
- **同键 + 同指纹**：始终返回同一个任务（`reused=true`），无论任务正在运行
  还是已结束，不重复计算、不会出现两份结果；
- **同键 + 不同指纹**：`409 idempotency_conflict`，错误体返回已绑定任务 id；
- 提交路径在同一把锁内完成“查重 → 建任务 → 落盘”，并发同键提交只会落一个任务。

## 结果可复现

求解器为确定性贪心：物品按价值密度降序（id 决定并列次序）处理，资源候选按剩余
容量加权并由 `random.Random(seed)` 选择。**相同输入 + 相同 `seed` 必然得到
相同的分配方案与目标值**；改变 `seed` 可能改变并列资源的选择。

## 本地验证

```bash
# 全量测试（22 个用例，覆盖并发提交、排队、取消、幂等重复、非法/不可行/无解、崩溃恢复）
uv run pytest -q

# 手工联调
uv run python main.py
curl -s http://127.0.0.1:8000/health
curl -s -X POST "http://127.0.0.1:8000/api/demo/problem?seed=1&n_items=5"
```

测试会断言：

- 并发上限下最多 N 个 running，超出任务保持 queued 并最终自动完成；
- 8 个并发同幂等键请求只产生 1 个任务；
- 排队/运行中任务取消后最终 `cancelled` 且 `result` 恒为 `null`；
- 成功/失败任务被取消时返回不同错误码，终态不回退；
- `invalid_input` / `infeasible` / `no_solution` 分别落到不同状态与原因；
- 相同输入相同 seed 的两个任务结果完全相等；
- 崩溃遗留的 running 在新进程中被重新排队并推进到终态。

日志统一打印**任务标识、状态变化与判定依据**，例如：

```
task=37972df6... 状态变化 queued -> running
task=37972df6... 状态变化 running -> succeeded（判定依据：求解完成，目标值 210.0）
task=ab12... 重启恢复：running -> queued（判定依据：上次运行未落终态）
```
