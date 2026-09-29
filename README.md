# 异步优化求解服务（FastAPI）

一个本地可跑的异步“资源分配”优化求解服务：用户提交算例后立即拿到任务标识，
后台异步求解；可随时查询进度、获取结果或取消任务。算例数据既可以显式提交，
也可以由服务端按种子确定性地生成假数据，不依赖任何外部数据库或第三方服务。

## 运行环境

- Python >= 3.14
- 依赖管理：[uv](https://docs.astral.sh/uv/)

## 启动方式

沿用项目原有入口 `main.py`（内部用 uvicorn 启动 FastAPI）：

```bash
uv sync
uv run python main.py
# 默认监听 127.0.0.1:8000
```

可用环境变量（均有默认值，不配也能跑）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | 监听地址 |
| `PORT` | `8000` | 监听端口 |
| `SOLVER_MAX_CONCURRENCY` | `2` | 同时运行的求解任务上限 |
| `TASK_STORE_PATH` | `tasks.json` | 任务持久化文件路径 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

示例：`PORT=8765 SOLVER_MAX_CONCURRENCY=4 uv run python main.py`

启动后可访问交互式文档：`http://127.0.0.1:8000/docs`。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/tasks` | 提交算例（支持 `idempotency_key`） |
| GET | `/api/tasks/{task_id}` | 查询任务状态 |
| GET | `/api/tasks/{task_id}/result` | 获取结果/失败原因 |
| POST | `/api/tasks/{task_id}/cancel` | 取消排队或运行中的任务 |

### 提交任务

请求体二选一提供数据来源：

- `instance`：显式算例，`items`（物品：需求/价值/是否必选）+ `resources`（资源：容量）；
- `generate`：本地假数据生成参数（物品数、资源数、取值范围、必选比例等）；省略时使用默认生成参数。
- `seed`：随机种子，**相同输入 + 相同 `seed` 必然得到相同解与目标值**；
- `max_attempts`：必选物品装载尝试轮数上限，调小可用于构造 `unsolvable` 场景。

```bash
curl -s -X POST http://127.0.0.1:8000/api/tasks \
  -H 'Content-Type: application/json' \
  -d '{"generate":{"num_items":20,"num_resources":3},"seed":42}'
# 202 -> {"task_id":"...","status":"queued","idempotent_replayed":false}
```

显式算例示例：

```json
{
  "instance": {
    "items": [
      {"item_id": "a", "demand": 3.0, "value": 5.0, "mandatory": true}
    ],
    "resources": [{"resource_id": "r1", "capacity": 10.0}]
  },
  "seed": 0
}
```

## 任务状态机

状态只能单向推进，不允许回退：

```
QUEUED ──> RUNNING ──> SUCCEEDED
   │          │
   │          └──────> FAILED          (invalid_input / infeasible / unsolvable)
   │          └──────> CANCELLED       (运行中被取消)
   └─────────────────> CANCELLED       (排队中被取消)

重启恢复：QUEUED 重新入队；RUNNING ──> FAILED(interrupted)
```

- `QUEUED`：已受理，在队列中等待求解槽位；
- `RUNNING`：求解器执行中；
- `SUCCEEDED` / `FAILED` / `CANCELLED`：终态，永不改变。

所有状态流转都在异步锁内判定并立即原子落盘，日志会打印**任务标识、状态变化与判定依据**，例如：

```
task=7639... 状态流转 QUEUED -> RUNNING
task=7639... 状态流转 RUNNING -> SUCCEEDED objective=57.16 依据=9 个分配
task=ab12... 状态流转 RUNNING -> FAILED code=infeasible 依据=必选物品总需求 ...
```

### 终态后的明确行为

| 场景 | HTTP | 返回中的原因 |
| --- | --- | --- |
| 对成功任务取消 | 409 | `detail.reason = already_succeeded` |
| 对失败任务取消 | 409 | `detail.reason = already_failed` |
| 重复取消已取消任务 | 200 | `reason = already_cancelled` |
| 排队/运行中取消成功 | 200 | `reason = cancelled` |
| 取消不存在的任务 | 404 | — |
| 任务未结束时取结果 | 409 | `detail.reason = not_finished` |
| 已取消任务取结果 | 409 | `detail.reason = result_discarded`（取消后绝不产生结果） |
| 成功任务取结果 | 200 | `status=succeeded` + `result` |
| 失败任务取结果 | 200 | `status=failed` + `failure_code`/`failure_reason` |
| 幂等键重复提交但内容不同 | 409 | 冲突，提示与首次提交不一致 |
| 语义非法的请求（见下） | 400 | 明确的错误描述 |
| 字段类型/约束不合法 | 422 | Pydantic 校验错误 |

## 失败原因分类（不混为一种失败）

| `failure_code` | 含义 | 典型判定依据 |
| --- | --- | --- |
| `invalid_input` | 输入语义非法 | `instance` 与 `generate` 同时给出；`item_id`/`resource_id` 重复等（提交时 400 拦截；求解阶段发现则落为 FAILED） |
| `infeasible` | 由必要条件即可判定不可行 | 某个必选物品需求超过最大单资源容量；必选物品总需求超过资源总容量 |
| `unsolvable` | 必要条件通过，但无法构造可行解 | 多轮确定性装载尝试后，必选物品仍无法全部装入（经典例子：必选需求 6/5/5、容量 8/8） |
| `cancelled` | 被用户取消 | 排队中或运行中收到取消 |
| `interrupted` | 进程重启中断 | 启动恢复时发现上次停留在 RUNNING |

非必选物品装不下时会被丢弃（出现在结果的 `unassigned_items` 中），任务仍为成功，
优化目标是被分配物品的价值之和。

## 并发与队列策略

- 启动时创建 `SOLVER_MAX_CONCURRENCY` 个 worker 协程，求解在独立线程中执行（`asyncio.to_thread`），不阻塞事件循环；
- 超出并发上限的任务**进入内存队列排队等待**，既不会直接失败也不会被丢弃；
- 取消通过每个任务独立的 `asyncio.Event` 下发：
  - 排队任务出队执行前二次检查取消标记，命中则直接 CANCELLED；
  - 运行中的任务在求解循环每个尝试轮次检查标记；
  - 求解返回、准备写入成功结果前做最后一次检查，**保证取消之后绝不落结果**；
- 取消与状态流转共用同一把异步锁，并发取消/完成之间不会出现状态回退或互相覆盖。

## 持久化与重启恢复

- 所有任务记录（含幂等键映射）以整表 JSON 写入 `TASK_STORE_PATH`，采用临时文件 + `os.replace` 原子替换，崩溃不会留下半截文件；
- 启动时：
  - 残留 `RUNNING` → 标记 `FAILED(interrupted)`，不会有任务永远停在运行中；
  - 残留 `QUEUED` → 重新入队继续求解；
  - 终态任务与幂等映射原样恢复，重启后同键提交仍命中同一任务；
- 优雅关闭时 worker 被取消、记录全部落盘；被 kill -9 时由下次启动的恢复逻辑兜底。

## 幂等规则

- 客户端可在提交时带 `idempotency_key`；
- 同一 key 的重复提交（请求体必须完全一致）始终返回**同一个 `task_id`**，不会重复计算，也不会产生两份结果；首次返回 `idempotent_replayed=false`，后续为 `true`；
- 同一 key 携带不同请求体返回 409 冲突，避免语义被悄悄覆盖；
- 不带 key 的提交每次创建新任务；
- 幂等映射持久化，进程重启后仍然生效；
- 注意幂等只保证“同一提交落同一任务”，结果本身的可复现由 `seed` 保证（相同输入 + 相同种子结果一致）。

## 求解模型与确定性

多背包式资源分配：必选物品必须全装、非必选物品可丢弃，单资源装载需求不得超过容量，
最大化已分配物品价值。求解器对必选物品先做降序 best-fit，随后使用由 `seed` 派生的
独立随机序列做多轮排列尝试；非必选物品按价值贪心补装。实例生成流与求解尝试流使用
不同的随机派生种子，互不干扰，因此**相同输入配合同一种子，解与目标值逐位一致**。

## 本地验证

运行全部测试（22 个用例，覆盖并发提交、取消、幂等重复提交、不可行/无解/非法输入、重启恢复）：

```bash
uv run pytest
```

测试构成：

- `tests/test_lifecycle.py`：完整生命周期、状态单向推进、终态稳定、相同种子结果一致；
- `tests/test_concurrency_cancel.py`：并发上限与排队、排队/运行中取消、取消不落结果、终态取消原因可区分；
- `tests/test_idempotency.py`：幂等重放、同键不同体冲突、8 路并发同键提交只建一个任务；
- `tests/test_failures.py`：infeasible / unsolvable / invalid_input 分类、422 字段校验、非必选物品丢弃；
- `tests/test_recovery.py`：崩溃后 RUNNING → interrupted、QUEUED 重放、幂等映射跨重启保留。

手工快速验证：

```bash
PORT=8765 uv run python main.py

# 提交
curl -s -X POST localhost:8765/api/tasks -H 'Content-Type: application/json' \
  -d '{"generate":{"num_items":20},"seed":42}'
# 查询 / 结果 / 取消（把 <id> 换成返回的 task_id）
curl -s localhost:8765/api/tasks/<id>
curl -s localhost:8765/api/tasks/<id>/result
curl -s -X POST localhost:8765/api/tasks/<id>/cancel
```
