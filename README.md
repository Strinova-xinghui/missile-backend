# `missile-backend` —— 等时线/等时面计算服务（阶段 C · W1）

把"等时线/等时面"从浏览器搬到服务端算：静态页面（`https://strinova-xinghui.github.io/…`）画点，
**等时线由本服务算并叠加**。**沙盒（图4）不在本服务范围**。

* 物理**一行都不在这里**：全部来自公开发布的求解器包 [`missile-solver`](https://github.com/Strinova-xinghui/missile-solver)（AGPL-3.0-only）。
* 游戏数据**不入库、不入镜像**：运行期由 `DATA_DIR` 指过去，启动时逐条哈希核对，**不通过就拒绝启动**。
* 许可：**AGPL-3.0-only**（根目录 `LICENSE` 与求解器仓逐字节同一份）。

---

## 1. 为什么是 FastAPI

契约是**纯 JSON 进出**的一小组只读计算接口，FastAPI 直接给到三样东西，Flask 都得自己写：

1. **声明式校验**：`keys` 数、`tier` 枚举、`iso_step` 下限都在 pydantic 模型上 ⇒ 非法请求在进业务前就变成 400；
2. **自动 schema**：`/openapi.json` + `/docs` —— W2（主仓契约）与 W3（前端）照着它对齐形状，不用口头同步；
3. **线程池卸载**：解算是 CPU 密集（一次 `solver.run()` 数十毫秒~秒级），用 `run_in_threadpool`/任务表把它挪出事件循环，同步请求与后台任务共用一个进程。

（Flask 也能做，但上面三条要各写一遍；这个仓的"薄"正来自"别自己造框架能力"。）

## 2. 数据（`DATA_DIR`，必须外部挂载）

```
DATA_DIR=<...>/inputs/resources/2.59.0.28     # 168 个 *.blk + presets.json + manifest.json
```

* 本机开发：`E:\导弹包线图-release\vendor\wt-missile\inputs\resources\2.59.0.28`
* 启动即校验：`missile_solver.verify_data(DATA_DIR)`；缺件/哈希不符 ⇒ 打印原因并**退出**（`app/physics.py::data_gate`）。
* **数据来源与免责声明**：这些 blk 是 War Thunder 的游戏数据（版权归 Gaijin Entertainment），
  **不在本仓、不在本服务镜像里**，也不随分发提供；使用者需自行准备合法副本并按 `DATA_DIR` 指过去。
  本服务只做数值计算，与 Gaijin 无关联、未获其授权或背书。

## 3. 接口（v1，冻结）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/v1/health` | `{ok, version, solver:{model,version,sha256}, data:{dir_ok,identity}, tier_default}` |
| GET | `/v1/catalog` | 与站点 `catalog.json` **同 schema**（求解器包 `catalog.build()` 原样） |
| POST | `/v1/overlay` | 等时线：`{kind:"plane"\|"bg", keys?, tier?, dv_pin?, iso_step?}` |
| POST | `/v1/isosurface` | 等时面：`{keys:[一枚], tier?, level_s?, grid?}` |
| POST | `/v1/jobs` | 长任务（全池/大网格）：`{job_id}` |
| GET | `/v1/jobs/{id}` | `{state:"running"\|"done"\|"error", progress, result?}` |
| GET | `/v1/estimate` | 成本预估（不跑解算）：这次会铺多大网格、能否走同步 |

`overlay` 的响应：

```json
{"kind":"plane","keys":["PL-12"],"standard":"PL-12",
 "axis":{"xlim":[804,1116],"ylim":[2500,3751],"x_step":20,"y_step":50},
 "iso_lines":[{"level":45.0,"points":[[x,y],…],"closed":false}],
 "t_range_s":[44.2,70.1],"hits":312,"nodes":384,"notes":[…],
 "identity":{"solver_sha256":"…","resource_sha256":"…","tier":"fast","backend":"compiled-c",
             "cached":false,"elapsed_ms":8123.4,"nodes":384}}
```

* **口径与主仓一致**：网格 `grid.make_grid()`（向内对齐；默认步长 10 / 25，**超过节点上限时按 2 倍逐级粗化**，
  响应里的 `axis.x_step/y_step` 会如实写出实际用的步长）；层级 `grid.iso_levels()`（整 `iso_step`）；
  等值线是**线性**等值线（主仓走 matplotlib `ax.contour`，两者在同一条线性场上给出同一批交点 ——
  `tests/test_api.py` 按"层级同值 + 点集合同值"钉住）。
* **与 W2 的关系**：主仓的 `workflow.overlay_plane/overlay_bg` 与 `python -m missile_sim --overlay … --json`
  **尚未落地**，因此 `app/overlay.py` 现在是"同形状的适配层"（自己铺网格 + 逐格点 `solver.run()`）。
  它们落地后，把 `app/overlay.py` 里的扫描换成"转发/包一层"即可，轴/层级口径不变，接口形状不动。
* **基准弹**：等时线属于 `STANDARD_MISSILE`（默认 `PL-12`，可被请求体覆盖）；`bg` 的固定 ΔV 默认取它标称值。
* `identity` 三项都能复现这次结果：`solver_sha256`（内核）、`resource_sha256`（数据哈希表指纹）、`tier`。

## 4. 本地起服务（真浏览器直连一页说明）

```powershell
$env:DATA_DIR = "E:\导弹包线图-release\vendor\wt-missile\inputs\resources\2.59.0.28"
$env:TIER_DEFAULT = "fast"          # 本机有编译好的 C 快路径时用它；否则用 standard
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8080
```

浏览器/前端直连：

```js
const r = await fetch("http://127.0.0.1:8080/v1/overlay", {
  method: "POST", headers: {"Content-Type": "application/json"},
  body: JSON.stringify({kind: "plane", keys: ["PL-12", "MICA"], tier: "fast", iso_step: 1})
});
```

* CORS 白名单默认 `https://strinova-xinghui.github.io` + `http://127.0.0.1:<任意端口>`（`ALLOW_LOCALHOST_ORIGINS=1`）；
  **没有 `*`** —— `CORS_ORIGINS` 里写 `*` 会直接抛错（`tests/test_api.py::test_cors_origin_list_rejects_wildcard`）。
* 同步请求有节点上限（`SYNC_MAX_NODES`，默认 400）：超了会 413 并提示走 `/v1/jobs`；
  拿不准就先 `GET /v1/estimate?kind=plane`。

## 5. 部署（数据不进镜像）

```powershell
docker build -t missile-backend .
docker run --rm -p 8080:8080 `
  -v "E:\导弹包线图-release\vendor\wt-missile\inputs\resources\2.59.0.28:/data/2.59.0.28:ro" `
  -e DATA_DIR=/data/2.59.0.28 -e TIER_DEFAULT=fast missile-backend
```

`fly.toml` 是等价的一份（volume `missile_data` 挂到 `/data`，`DATA_DIR=/data/2.59.0.28`）。
**注意**：容器里通常没有 C 编译器 ⇒ `tier=fast` 会退化成纯 Python（`fallback_reason` 会写明），
大网格请走 `/v1/jobs`，或把 `SYNC_MAX_NODES` 调小。

## 6. 环境变量一览

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DATA_DIR` | 空（**必填**） | 168 blk 所在目录；启动校验 |
| `TIER_DEFAULT` | `fast` | `standard` / `fast` |
| `STANDARD_MISSILE` | `PL-12` | 等时线的基准弹（池内短名） |
| `CORS_ORIGINS` | 站点域名 | 逗号/空格分隔的**显式**白名单（不许 `*`） |
| `ALLOW_LOCALHOST_ORIGINS` | `1` | 是否额外允许 `http://127.0.0.1:<port>` |
| `MAX_KEYS` | `168` | 单请求弹数上限 |
| `MIN_ISO_STEP` | `0.5` | `iso_step` 下限 [s] |
| `SYNC_MAX_NODES` / `JOB_MAX_NODES` | `400` / `20000` | 同步 / 任务允许的网格节点数 |
| `RATE_LIMIT_PER_MIN` / `RATE_LIMIT_BURST` | `60` / `12` | 按 IP 的令牌桶；超限 429 + `Retry-After` |
| `JOB_WORKERS` / `JOB_TTL_S` | `2` / `3600` | 任务线程数 / 结果保留时长 |
| `CACHE_DIR` / `CACHE_MAX_ITEMS` | 空 / `32` | 磁盘缓存目录（给了就落盘）/ 进程内 LRU 容量 |

## 7. 自测

```powershell
$env:DATA_DIR = "E:\导弹包线图-release\vendor\wt-missile\inputs\resources\2.59.0.28"
python -m pytest tests -q     # 12 项：健康+数据门 / 目录 schema / 等时线形状与数值 / 缓存 / CORS / 限流 / 任务状态机
```

判据都是**真跑求解器**（小窗口，秒级）；只有"数据门"那条故意喂坏数据。
