# 入口服务故障定位 API

针对「入口服务不可用」告警，判断故障出在**入口（gateway）**、**鉴权（auth）**、
**订单（order）**，还是**鉴权与订单共用的缓存（cache）**。

- 诊断 API：FastAPI + SQLite，Swagger 见 `http://localhost:8000/docs`
- 四个模拟服务：可由 API 设置本地故障（liveness / readiness / 探测延迟），
  并提供 `/health/live`、`/health/ready`、`/admin/*` 探测与管理接口
- 拓扑：`gateway -> {auth, order}`，`auth -> cache`，`order -> cache`
- 启动：Docker Compose，五个容器（四个模拟服务 + 诊断 API）

## 快速开始

```bash
docker compose up --build -d
# Swagger UI
open http://localhost:8000/docs

# 1) 健康状态
curl -s -X POST 'http://localhost:8000/diagnosis?wait=true' | jq .

# 2) 注入缓存就绪故障（会递增全局状态版号）
curl -s -X PUT http://localhost:8000/faults/cache \
  -H 'Content-Type: application/json' -d '{"ready_ok": false}' | jq .

# 3) 诊断：缓存是唯一根因，auth/order/gateway 为受下游牵连
curl -s -X POST 'http://localhost:8000/diagnosis?wait=true' | jq '
  {outcome: .result.outcome,
   root_causes: .result.root_causes,
   independent_faults_count: .result.independent_faults_count,
   affected: .result.affected_services}'

# 4) 清空故障
curl -s -X POST http://localhost:8000/faults/reset | jq .

# 演练：cache + gateway 两个独立根因，只清除 cache，gateway 保持故障
curl -s -X PUT http://localhost:8000/faults/cache   -H 'Content-Type: application/json' -d '{"ready_ok": false}' >/dev/null
curl -s -X PUT http://localhost:8000/faults/gateway -H 'Content-Type: application/json' -d '{"live_ok": false}' >/dev/null
BATCH=$(curl -s -X POST 'http://localhost:8000/diagnosis?wait=true')
curl -s -X POST http://localhost:8000/faults/clear-root-cause \
  -H 'Content-Type: application/json' \
  -d "$(echo "$BATCH" | jq -c '{batch_id, state_version, target_service:"cache"}')" | jq \
  '{status, target_service, remaining_root_causes: [.remaining_root_causes[].service], reasons}'
```

本地不装 Docker 时，可直接跑同一套代码（依赖见 `requirements.txt`）：

```bash
export GATEWAY_URL=http://127.0.0.1:8001 AUTH_URL=http://127.0.0.1:8002 \
       ORDER_URL=http://127.0.0.1:8003 CACHE_URL=http://127.0.0.1:8004
python -m uvicorn app.services.gateway:app --port 8001 &
python -m uvicorn app.services.auth:app    --port 8002 &
python -m uvicorn app.services.order:app   --port 8003 &
python -m uvicorn app.services.cache:app   --port 8004 &
DIAG_DB_PATH=/tmp/diagnosis.db python -m uvicorn app.main:app --port 8000 &
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/diagnosis?wait=true\|false&wait_seconds=` | 并发探测四个服务并给出诊断。`wait=true` 有界等待，超时返回 `202` + 批次号；`wait=false` 立即返回 `202` |
| `GET` | `/diagnosis/{batch_id}` | 单个批次结果；未完成的批次返回 `running` |
| `GET` | `/diagnosis/latest` | 最新批次（默认包含未完成批次；`?completed_only=true` 只看有结论的批次） |
| `GET` | `/diagnosis?limit=` | 最近批次列表 |
| `PUT` | `/faults/{service}` | 设置 `live_ok` / `ready_ok` / `probe_delay_ms`，**递增全局状态版号** |
| `POST` | `/faults/reset` | 清空所有本地故障，**递增全局状态版号** |
| `POST` | `/faults/clear-root-cause` | 按诊断批次**只清除一个服务**的本地根因，随后触发新诊断核对（乐观并发，见下） |
| `GET` | `/state` | 全局状态版号、批次序号、各服务采用的版号与配置轮次 |
| `GET` | `/topology` | 依赖拓扑 |
| `GET` | `/health` | 诊断 API 自身存活 |

可选乐观并发控制：`PUT /faults/{service}` 与 `POST /faults/reset` 接受
`X-Expected-State-Version` 头（或同名 query 参数）；与当前版号不符返回 `409`，
且不产生任何副作用。

模拟服务各自的 Swagger 在 `:8001/docs`、`:8002/docs`、`:8003/docs`、`:8004/docs`。

## 诊断语义

一次诊断批次（batch）的过程：

1. **快照**全局状态版号 `V`，创建批次记录（`running`，SQLite）。
2. 读取四个服务的 `config_epoch` 作为基线。
3. **并发**探测每个服务的 `/health/live` 与 `/health/ready`；每个 HTTP 调用有
   单次超时（`DIAG_PROBE_TIMEOUT`，默认 2s），整批还有外层截止时间，未完成的
   探测记为 timeout。
4. 纯函数归约（`app/diagnosis.py`）产出结论。

### 何谓根因 / 受牵连

- **根因**：服务自身 liveness 失败，或自身 readiness 失败（本地 readiness 标志为
  false），或进程不可达。
- **受牵连（collateral）**：liveness 正常、本地 readiness 正常，仅因依赖未就绪
  而未就绪。每个受牵连服务记录 `caused_by` 与 `dependency_paths`（如
  `gateway -> auth -> cache`）。
- **缓存只算一次**：`cache` 故障会同时打挂 `auth`、`order`，进而打挂
  `gateway`。归约时按依赖图的根因收敛，`independent_faults_count = 1`，
  `primary_root_cause = "cache"`，其余三个服务进入 `affected_services`。
  只有真正独立的本地下故障才增加计数（例如 cache 故障 + gateway 自身 liveness 故障
  = 2）。

### 待重测（`needs_retest`）

以下任一情况**不给根因**，返回 `outcome = "needs_retest"`、`root_causes = []`，
并在 `retest_reasons` 中列明原因：

- 任一探测超时，或响应不可解析；
- 探测结果混有不同 `state_version`（服务间不一致，或同一服务 live/ready 不一致，
  或与批次期望版号不一致）；
- 探测期间全局状态版号发生变化；
- 探测期间某服务 `config_epoch` 变化（配置在测量中途被改动）；
- 某服务在依赖检查中等待超时（说明依赖的响应性未被证明）；
- 证据自相矛盾：例如 readiness 声称某依赖未就绪，但该依赖自身探测通过；
  或触发了未声明的依赖；
- 批次被重启打断 / 启动前已失效。

### 旧批次不覆盖新结果

- 每个批次有单调递增的 `seq`（持久化）。
- 若某批次的探测在更年轻的批次已创建之后才结束，落库时标记
  `superseded=true` 与 `superseded_by=<新批次 seq>`，仅作审计；
  `GET /diagnosis/latest` 按 `seq` 取最新，因此旧批次永不覆盖新结论。
- 互不相关的更早批次不会被追溯标记，只有真正「迟到」的批次才带该标记。

### 重启

- 已完成批次写入 SQLite，重启后可继续查询（`GET /diagnosis/{id}`、
  `/diagnosis/latest?completed_only=true`）。
- 重启时仍为 `running` 的批次由生命周期钩子标记为
  `status=interrupted`、`outcome=needs_retest`，并说明「进程重启、探测未知，需重测」。
- `state_version`、`batch_seq` 也持久化，重启后不会重发已用过的版号/序号。

### 设置故障即递增版号

`PUT /faults/{service}` 与 `POST /faults/reset` 都在全局锁内：

1. `state_version += 1` 并持久化；
2. 先把新版本同步到全部四个服务（`POST /_fault/sync`）；
3. 再应用本地故障（目标服务的 `config_epoch` 随之递增）；
4. 返回新版本、旧版本、各服务状态与警告（若有服务不可达导致同步失败，
   会明确提示「此时诊断只会给出待重测」）。

若有人绕过诊断 API 直接调用模拟服务的 `PUT /_fault`，后台监视器（默认 0.5s）
通过 `config_epoch` 变化发现改动，同样递增并同步全局版号（探测进行中会跳过，
下一轮补上）。

### 按诊断批次清除单个根因（`POST /faults/clear-root-cause`）

值班演练中同时注入两个独立本地故障（例如 cache 就绪故障 + gateway 存活故障）、
诊断确认两个根因后，可用该 API **只清除其中一个**，另一个保持故障。请求体：

```json
{ "batch_id": "<所见诊断批次>", "state_version": <批次全局版号>, "target_service": "cache" }
```

可带 `?wait_seconds=` 控制等待验证诊断的预算（默认 `DIAG_PROBE_TIMEOUT + 8s`）。

**处理范围（只处理最新已完成诊断中、可达的本地存活或就绪故障）：**

- 批次必须存在且 `completed`，并且是当前**最新的已完成批次**；
- 批次必须是 `root_cause_found`，且目标服务确实在其 `root_causes` 中；
- 只接受 `failure = liveness | readiness` 且证据为可达 HTTP 响应的根因；
  `unreachable` 根因没有管理端口可清，直接拒绝；受牵连（collateral）服务也拒绝；
- 清除动作只调用目标服务的 `POST /_fault/reset`，**绝不触碰其他服务的故障**。

**旧请求一律拒绝，且拒绝发生在任何写入之前（404/409，无副作用）：**

- 批次不存在 → `404 batch_not_found`；批次未完成 → `409 batch_not_completed`；
- 已有更新的已完成诊断，或提交版号与批次版号不符 → `409 stale_batch`；
- 请求提交后全局版号已变化 → `409 state_version_conflict`；
- 目标服务的 `config_epoch` 与批次证据不一致（说明期间有人直改/重设过该服务）
  → `409 target_config_changed`；
- 目标服务当前不可达 → `409 target_unreachable`；目标不是可清根因 →
  `409 target_not_root_cause` / `not_root_cause_verdict` 等。

**清除后的核对回执：** 守卫通过后，在全局锁内完成「版号 +1 → 全量同步 →
仅重置目标服务」，释放锁再触发一个全新诊断批次。回执 `status` 只有两种：

- `cleared`：验证诊断证据与当前配置一致（版号、目标 `config_epoch` 未再变化），
  且目标**不再是根因**；其余根因在 `remaining_root_causes` 中照常报告；
- `pending_verification`：清除回执不明（管理端重置未确认）、验证探测超时/
  `needs_retest`、或验证期间版号/目标配置再次变化。此时只报待核实并给出
  `reasons` 与 `verification_batch_id`，可轮询 `GET /diagnosis/{id}` 自行复核。

**重复旧请求不得改动后来设置的故障：** 由于批次与版号守卫，同一份旧请求重放
必然命中 `stale_batch` / `state_version_conflict` / `target_config_changed`，
不会再执行任何重置；后来直改或通过 API 设置的故障原样保留。

## 目录结构

```
app/
  config.py       拓扑、依赖图、环境配置
  mock_service.py 四个模拟服务的共用实现
  services/       各服务的 uvicorn 入口
  probes.py       HTTP 探测与证据归一化
  diagnosis.py    纯函数归约：根因 / 受牵连 / 待重测
  coordinator.py  状态版号、故障注入、批次调度、重启恢复、外部故障监视
  store.py        SQLite 持久化
  models.py       Pydantic 模型（驱动 OpenAPI）
  main.py         FastAPI 应用
tests/
  test_reduction.py  归约逻辑单元测试（14）
  test_clear_guard.py 单根因清除守卫纯函数单元测试（15）
  test_api.py        真实 uvicorn 进程端到端测试（19）
  test_clear_api.py  单根因清除 API 端到端测试（11）
```

## 测试

```bash
python -m pip install -r requirements.txt pytest
python -m pytest -q
```

测试用随机端口启动 4 个模拟服务与诊断 API 的真实进程，覆盖：健康、四类故障、
缓存故障去重、双独立故障、超时、版号混杂、依赖检查超时、并发探测、异步批次与
迟到响应、等待预算到期返回 202 后轮询、重启恢复、版号冲突、Swagger/OpenAPI、
未知服务，以及单根因清除的双根因演练、验证诊断审计、旧请求重放不改新故障、
版号/配置漂移拒绝、受牵连与不可达目标拒绝、待核实回执（等待预算到期、
验证探测超时）与 OpenAPI 暴露。

## 已知限制

- 协调器按**单实例**设计（一个诊断 API 容器）。SQLite 文件 + 进程内锁；多副本
  部署需要换成共享存储与分布式锁。
- 重启恢复依赖 CSS/容器已被杀掉后重新拉起；`running` 批次的探测不复用。
- Docker 镜像与 Compose 文件未在本机运行（环境无 Docker），已用同代码的本地进程
  做了等价验证，详见交付说明。
