# Azure 托管方案评估（2026-10-09 定稿）

> 起因：用户问「听说 Azure 有更适合的 functions 可以托管，不需要占用云服务器」。
> 结论：**Azure Functions 不适合本服务**，理由是架构层面的（不是配置问题）。Azure 侧的正解是
> **Container Apps（缩容到零）**，或者**与卫戍协议共用那台免费 VM**。
> 全部数字按官方文档 + Azure 零售价 API 实测（东亚区，2026-10-09）。

---

## 1. 先决问题：这个镜像此前从未成功启动过

`Dockerfile` 第 28 行的 `--port` 后面**变量是空的**（初始提交 `cb77745` 引入，从未修过）：

```dockerfile
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port "]   # ← 旧
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]  # ← 已修
```

后果：`uvicorn: error: argument --port: expected one argument` ⇒ **进程立刻退出**。
即任何平台（Fly / ACA / VM）拿到这个镜像都起不来。**已修复，是本轮唯一的代码改动。**
（本机无 `docker` CLI，未能实机 build 验证；已用等价 argparse 复现/复验。）

---

## 2. 为什么 Functions 不行（三条硬约束，按致命程度排序）

### 2.1 进程内状态 × 多实例 = 功能直接坏掉

`app/main.py:28-30` 是**模块级单例**，全部状态活在单个进程的内存里：

```python
CACHE  = ResultCache()   # 进程内 LRU（CACHE_MAX_ITEMS=32）
BUCKET = TokenBucket()   # 按 IP 的令牌桶（60/min，burst 12）
JOBS   = JobStore()      # ThreadPoolExecutor(2) + 内存 dict，TTL 3600s
```

Functions 是**事件驱动横向扩缩**（Flex Consumption 上限 1000 实例）。后果：

- **`/v1/jobs` 必然失效**：`POST /v1/jobs` 落在实例 A，轮询 `GET /v1/jobs/{id}` 被路由到实例 B
  ⇒ `JOBS.get()` 返回 `None` ⇒ 404 `job_not_found`。这是**大概率**而非偶发。
- **限流形同虚设**：`TokenBucket` 每个实例各记一份，N 个实例 = 实际配额 ×N。
- **缓存命中率崩塌**：`ResultCache` 不共享，重复计算全打到 CPU 上。

要修就得外置状态（Redis / 表存储 / 队列），而 `JobStore` 还带着 `ThreadPoolExecutor` 的
本地执行语义 —— 等于把 `/v1/jobs` 整条链路重写成「入队 + 轮询 + 结果落盘」。
**那是重写服务，不是部署服务。**

### 2.2 推荐方案 Flex Consumption **不支持容器**

官方 [Functions 托管方案对照](https://learn.microsoft.com/en-us/azure/azure-functions/functions-scale) 明写：

| 托管方案 | 容器支持 |
| --- | --- |
| Flex Consumption（**新项目推荐**） | ❌ **None** |
| Premium | ✅ Linux |
| Dedicated | ✅ Linux |
| Container Apps | ✅ Container-only |
| Consumption（**legacy**，Linux 已退役） | ❌ None |

本仓交付物是 `Dockerfile`（`FROM python:3.13-slim` + `pip install missile-solver`）。
选 Flex Consumption ⇒ 必须改写成「code-only 部署」，且 Functions 的编程模型是
**一个函数一个 handler**，不是「一整个 ASGI 应用」。

即便用 `azure.functions.AsgiMiddleware` / `AsgiFunctionApp` 把 FastAPI 包进单个 HTTP trigger，
也解决不了 2.1（状态）、2.3（超时），而且 `lifespan` 的启动闸门语义会变。

### 2.3 CPU 密集 + HTTP 硬上限 230 秒

官方原文（同一页）：

> Regardless of the function app timeout setting, **230 seconds is the maximum** amount of time that an
> HTTP triggered function can take to respond to a request. This limit exists because of the default
> idle timeout of Azure Load Balancer.

而本服务的计算量：一次 `solver.run()` 数十毫秒到数秒；同步请求上限 `SYNC_MAX_NODES=400`，
更大的走 `/v1/jobs`（`JOB_MAX_NODES=20000`）。20000 节点的大网格在**纯 Python**（容器里通常
没有 C 编译器 ⇒ `tier=fast` 静默退化，见 README §5 的警告）下**很可能超过 230 秒**。

另外 Functions 的 Consumption 档默认超时仅 **5 分钟 / 上限 10 分钟**，Premium 才 unbounded。

### 2.4 附带伤害：冷启动

`lifespan` 第一步是 `physics.data_gate()` —— 启动即校验 168 个 `.blk` 的哈希，不通过就
`raise` ⇒ uvicorn 退出（**fail-closed**，设计正确）。Functions 缩容到零后每次唤醒都要：
拉容器 → import numpy → 校验 168 个文件哈希 → 才接第一个请求。
用户感知就是「点了没反应十几秒」。

---

## 3. Azure 侧的正解

### 方案 A：Azure Container Apps（Consumption）— **推荐**

支持 Docker 镜像 + 缩容到零，免费额度（每订阅每月）：

- 前 **180,000 vCPU·秒**
- 前 **360,000 GiB·秒**
- 前 **200 万次** HTTP 请求

**关键换算**（东亚实测单价：vCPU 活跃 `$2.4e-05/s`、vCPU 空闲 `$3e-06/s`、内存 `$3e-06/GiB·s`）：

| 配置 | 用量 | 是否超免费额度 |
| --- | --- | --- |
| 0.5 vCPU + 1 GiB × **100 h/月** | 180,000 vCPU·s / 360,000 GiB·s | ✅ **正好用满，$0** |
| 0.5 vCPU + 1 GiB × 200 h/月 | 360,000 / 720,000 | ❌ 超 $5.40/月（全活跃价） |
| 0.5 vCPU + 1 GiB 常驻 730 h | 1,314,000 / 2,628,000 | ❌ 超 $34.02/月 ≈ $408/年 |
| 0.25 vCPU + 0.5 GiB 常驻 730 h | 657,000 / 1,314,000 | ❌ 超 $14.31/月 ≈ $172/年 |

⇒ **0.5 vCPU + 1 GiB 恰好免费跑 100 小时/月**（约 3.3 小时/天）。
配合 `minReplicas=0`，只要每月真实使用不超过 100 小时就**完全免费**；
超过就按上表线性付费（还有 idle 档单价更低，但保守按活跃价估）。

**但注意**：ACA 缩容到零同样会**多实例**，2.1 的状态问题在这里**依然存在**。
所以要么 `minReplicas=maxReplicas=1` 锁单实例（那就不是「免费 100 小时」而是常驻计费），
要么接受状态问题的修法。**这是本方案唯一的坑，必须在动手前想清楚。**

### 方案 B：与卫戍协议共用那台免费 VM — **最省**

学生订阅已含 **750 h/月**的 B1s / B2pts_v2(Arm) / B2ats_v2(AMD)，750 h ≈ 31.25 天 ⇒ 可 24/7 常驻，
**只有磁盘收费**（P6 64 GiB ≈ `$0.73/月` ≈ `$8.76/年`）。

既然卫戍协议迁移**已经要开这台 VM**，missile-backend 直接在同一台机上 `docker run` 即可：

- **零新增成本**、零新增服务、零新增免费额度占用。
- 数据卷直接挂本机目录，`DATA_DIR` 语义不变。
- 单实例 ⇒ **2.1 的状态问题不存在**。
- 风险：B2ats_v2 只有 **1 GiB 内存**。Node 游戏服约 200–400 MB + Python/numpy + 168 blk
  可能接近上限 ⇒ 建议加 swap，或让两者按需错峰启动。

### 方案 C：Fly.io（现有 `fly.toml`）

`fly.toml` 已经写好（`primary_region="nrt"`、`auto_stop_machines="suspend"`、`min_machines_running=0`、
`shared-cpu-2x` / 2 GB）。这是**改动最小的路** —— 前提是账号与计费可用。
与 Azure 方案不冲突，可作备份路线。

---

## 4. 建议

1. **不要为这个服务选 Functions。** 架构不匹配（有状态 + CPU 密集 + 容器），不是配置能绕过的。
2. **首选方案 B**（与卫戍协议共用 VM）：零成本、单实例、状态问题天然消失。等 10-11 学生认证
   解锁、VM 开好后一起部署。
3. **次选方案 A**（ACA）：如果希望后端独立、且能接受「锁单实例 = 常驻计费」或「改造成无状态」。
4. **部署前必做**：本仓 Dockerfile 的 `--port` bug 已修（见 §1），但**必须在真实
   `docker build` 环境验证一次**（本机无 docker CLI）。
5. **数据不入镜像**这条底线继续遵守：`.blk` 是 War Thunder 数据（© Gaijin），
   `.gitignore` 已排除，运行期靠 `DATA_DIR` 挂载。

---

## 5. 遗留 / 未决

- **前端还没接后端**：`E:\导弹包线图-ghpages` 里目前**没有任何指向本服务的 `fetch`**
  （只有 `fetch("./catalog.json")` / `fetch("./notes.json")`），W3 尚未开始。
  ⇒ 部署的紧迫性不高，可以等前端接上再定后端形态。
- **AGPL-3.0-only**：二次分发/托管需注意许可证义务（本仓 `pyproject.toml` 已声明）。
- 本机**无 `docker` CLI** ⇒ 无法本地 build/run 验证镜像，这是所有方案的共同前置风险。
- Functions 免费额度的具体数字官方定价页未静态列出（按 GB-s + 执行次数计费），
  未影响本结论（决定性因素是架构，不是价格）。
