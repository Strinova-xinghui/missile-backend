# Azure 托管方案评估（2026-10-09 定稿）

> 起因：用户问「听说 Azure 有更适合的 functions 可以托管，不需要占用云服务器」。
> 结论：**Azure Functions 不适合本服务**，理由是架构层面的（不是配置问题）。Azure 侧的正解是
> **与卫戍协议共用那台免费 VM**（首选），或 **Container Apps 锁单实例**（次选）。
> 数字来源：Microsoft Learn / Azure 定价页 / Azure 零售价 API（东亚区实测），2026-10-09。
> 本结论经**两路独立核实**（主 agent + 独立研究员），逐条一致。

---

## 1. 先决问题：这个镜像此前从未成功启动过

`Dockerfile` 第 28 行的 `--port` 后面**变量是空的**（初始提交 `cb77745` 引入，从未修过）：

```dockerfile
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port "]   # ← 旧
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]  # ← 已修
```

后果：`uvicorn: error: argument --port: expected one argument` ⇒ **进程立刻退出**。
即任何平台（Fly / ACA / VM）拿到这个镜像都起不来。**已修复（提交 `6058a5d`）。**
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

Functions 是**事件驱动横向扩缩**（Consumption 上限 100/200 实例，Flex 上限 1000）。后果：

- **`/v1/jobs` 必然失效**：`POST /v1/jobs` 落在实例 A，轮询 `GET /v1/jobs/{id}` 被路由到实例 B
  ⇒ `JOBS.get()` 返回 `None` ⇒ 404 `job_not_found`。这是**大概率**而非偶发。
- **限流形同虚设**：`TokenBucket` 每个实例各记一份，N 个实例 = 实际配额 ×N。
- **缓存命中率崩塌**：`ResultCache` 不共享，重复计算全打到 CPU 上。

官方对此是**明文要求**（非暗示）：

> Functions should be **stateless** and idempotent if possible. Associate any required state information
> with your data.
> — [performance-reliability](https://learn.microsoft.com/en-us/azure/azure-functions/performance-reliability)

> Because Functions doesn't track these background threads, **site shutdown can occur regardless of
> background thread status**... may be preempted by site shutdown, **leaving that work in an unknown state.**
> — 同上

第二条**逐字命中** `JobStore` 的 `ThreadPoolExecutor` 后台任务模型。
另有官方仓库 issue 佐证 ASGI 短板：`azure-functions-python-worker#911`（ASGI startup events）、
`#1782`（FastAPI background tasks 在 Functions 上不工作）。

要修就得外置状态（Redis / 表存储 / 队列），而 `JobStore` 还带着 `ThreadPoolExecutor` 的
本地执行语义 —— 等于把 `/v1/jobs` 整条链路重写成「入队 + 轮询 + 结果落盘」。
**那是重写服务，不是部署服务。**

### 2.2 推荐方案 Flex Consumption **不支持容器**

官方 [Functions 托管方案对照](https://learn.microsoft.com/en-us/azure/azure-functions/functions-scale) 明写：

| 托管方案 | 容器支持 | 单实例内存 | 最大实例 |
| --- | --- | --- | --- |
| Flex Consumption（**新项目推荐**） | ❌ **None** | 4 GB | 1000 |
| Premium | ✅ Linux | 3.5–14 GB | — |
| Dedicated | ✅ Linux | 1.75–256 GB | — |
| Container Apps | ✅ Container-only | — | — |
| Consumption（**legacy**，Linux 已退役） | ❌ None | ~1.5 GB / 1 CPU | 100 / 200 |

本仓交付物是 `Dockerfile`（`FROM python:3.13-slim` + `pip install missile-solver`）。
选 Flex Consumption ⇒ 必须改写成「code-only 部署」。虽然官方**确实支持**用
`azure.functions.AsgiMiddleware` / `AsgiFunctionApp` 包装 FastAPI（有官方 sample
[fastapi-on-azure-functions](https://github.com/Azure-Samples/fastapi-on-azure-functions)），
但那解决不了 2.1（状态）与 2.3（超时），且 `lifespan` 语义会变。

### 2.3 CPU 密集 + HTTP 硬上限 230 秒

官方原文（[functions-scale](https://learn.microsoft.com/en-us/azure/azure-functions/functions-scale) 脚注）：

> Regardless of the function app timeout setting, **230 seconds is the maximum** amount of time that an
> HTTP triggered function can take to respond to a request.

**这个上限与托管方案无关**（Consumption / Flex / Premium 都一样）。
单次执行超时：Consumption 默认 5 分钟、上限 10 分钟；Flex/Premium 默认 30 分钟、无上限 ——
但 HTTP 触发始终卡在 230 秒。

而本服务的计算量：一次 `solver.run()` 数十毫秒到数秒；同步请求上限 `SYNC_MAX_NODES=400`，
更大的走 `/v1/jobs`（`JOB_MAX_NODES=20000`）。20000 节点的大网格在**纯 Python**（容器里通常
没有 C 编译器 ⇒ `tier=fast` 静默退化，见 README §5 的警告）下**很可能超过 230 秒**。

> 对照：**Container Apps 的请求超时是 240 秒**（[ingress-overview](https://learn.microsoft.com/en-us/azure/container-apps/ingress-overview)），
> 且 Premium ingress 可调到 30 分钟（需 ≥2 个 Dedicated 节点）。ACA 略优于 Functions，但都
> 不足以承载 20000 节点的大网格 —— **长任务本来就该走 `/v1/jobs`**，这一点现有设计是对的。

### 2.4 附带伤害：冷启动

`lifespan` 第一步是 `physics.data_gate()` —— 启动即校验 168 个 `.blk` 的哈希，不通过就
`raise` ⇒ uvicorn 退出（**fail-closed**，设计正确）。Functions 缩容到零后每次唤醒都要：
拉容器 → import numpy → 校验 168 个文件哈希 → 才接第一个请求。
用户感知就是「点了没反应十几秒」。（官方未公布 Python 冷启动秒数，此处不臆测具体数字。）

---

## 3. Azure 侧的正解

### 方案 A：与卫戍协议共用那台免费 VM — **首选**

学生订阅已含 **750 h/月**的 B1s / B2pts_v2(Arm) / B2ats_v2(AMD)，750 h ≈ 31.25 天 ⇒ 可 24/7 常驻，
**只有磁盘收费**（P6 64 GiB ≈ `$0.73/月` ≈ `$8.76/年`）。

既然卫戍协议迁移**已经要开这台 VM**，missile-backend 直接在同一台机上 `docker run` 即可：

- **零新增成本**、零新增服务、零新增免费额度占用。
- 数据卷直接挂本机目录，`DATA_DIR` 语义不变。
- **单实例 ⇒ 2.1 的状态问题不存在**（一行代码都不用改）。
- 无 230/240 秒请求上限、无冷启动。
- 风险：B2ats_v2 只有 **1 GiB 内存**。Node 游戏服约 200–400 MB + Python/numpy + 168 blk
  可能接近上限 ⇒ 建议加 swap，或让两者按需错峰启动。

### 方案 B：Azure Container Apps（Consumption）— **次选**

支持任意 `linux/amd64` Docker 镜像 + 缩容到零。免费额度（每订阅每月）：

- 前 **180,000 vCPU·秒**
- 前 **360,000 GiB·秒**
- 前 **200 万次** HTTP 请求

学生页明确把这三项列为 Always 免费（[Azure for Students](https://azure.microsoft.com/en-us/free/students)）。

**关键换算**（东亚实测单价：vCPU 活跃 `$2.4e-05/s`、vCPU 空闲 `$3e-06/s`、内存 `$3e-06/GiB·s`）：

| 配置 | 用量 | 是否超免费额度 |
| --- | --- | --- |
| 0.5 vCPU + 1 GiB × **100 h/月** | 180,000 vCPU·s / 360,000 GiB·s | ✅ **正好用满，$0** |
| 0.5 vCPU + 1 GiB × 200 h/月 | 360,000 / 720,000 | ❌ 超 $5.40/月（全活跃价） |
| 0.5 vCPU + 1 GiB 常驻 730 h | 1,314,000 / 2,628,000 | ❌ 超 $34.02/月 ≈ $408/年 |
| 0.25 vCPU + 0.5 GiB 常驻 730 h | 657,000 / 1,314,000 | ❌ 超 $14.31/月 ≈ $172/年 |

⇒ **0.5 vCPU + 1 GiB 恰好免费跑 100 小时/月**（约 3.3 小时/天）。

**⚠️ 本方案的死结：进程内状态与「免费」不可兼得。**

- `minReplicas=0` ⇒ 免费（≤100 h/月），但**多实例** ⇒ 2.1 的状态问题照样存在，必须改造。
- `minReplicas=maxReplicas=1` ⇒ 锁单实例、状态问题消失，但**变成常驻计费**
  ⇒ 按上表 0.5 vCPU+1 GiB 就是 `$34/月`，$100 额度撑不到 3 个月。

另两个 ACA 细节：

- **数据挂载**：可用 Azure Files（SMB/NFS）挂 168 个 `.blk`（仅 2.8 MB）
  （[storage-mounts-azure-files](https://learn.microsoft.com/en-us/azure/container-apps/storage-mounts-azure-files)）。
  比 Functions 更省心（Functions 只有 Flex 支持挂载，且 Flex 不支持容器 ⇒ 二选一，死锁）。
- **缩容到零的前提**：官方脚注 —— *"Applications that scale on CPU or memory load can't scale to zero."*
  本服务是 HTTP 触发（非 CPU/内存触发），**可以**缩容到零，这条对我们无碍。

### 方案 C：Fly.io（现有 `fly.toml`）

`fly.toml` 已经写好（`primary_region="nrt"`、`auto_stop_machines="suspend"`、`min_machines_running=0`、
`shared-cpu-2x` / 2 GB）。这是**改动最小的路** —— 前提是账号与计费可用。与 Azure 方案不冲突，可作备份。

### 为什么不选 Functions：免费额度也救不了

Functions 确实有免费额度（[定价页](https://azure.microsoft.com/en-us/pricing/details/functions/)）：

| 方案 | 免费额度/月 |
| --- | --- |
| Consumption（legacy） | 100 万次 + 400,000 GB-s |
| Flex Consumption | 25 万次 + 100,000 GB-s |

**但这对本服务没有意义** —— 决定性因素是架构（2.1–2.3），不是价格。
另外定价页有一条脚注需警惕：*"Free grants apply to the on-demand meters on **paid, consumption
subscriptions only**."* 学生订阅是否算 paid consumption subscription，措辞不直白，需门户实测账单。
（**缓解证据**：学生页 Compute 分类下明确列出 `Azure Functions — 1 million requests`，标签 **Always** —— 额度数与
Consumption 档的 100 万次一致，倾向于「适用」；但页脚注措辞仍未消除歧义，**以门户实开一台验证账单为 $0 为准**。）
（Premium 档绝无免费可能：东亚实测 EP1 = vCPU `$0.20/h` + 内存 `$0.01/GiB·h`，
1 vCPU + 3.5 GiB 常驻一个月 ≈ **$171.55**。）

---

## 4. ⚠️ 实操风险：学生订阅有区域限制

Azure for Students 受 **Allowed resource deployment regions** 策略约束，**通常只开放约 5 个区域**。
这直接影响能否选到**东亚 / 东南亚**这类低延迟区域（[相关 Q&A](https://learn.microsoft.com/en-us/answers/questions/5566650/which-regions-are-allowed-for-my-azure-student-sub)）。

**动手前第一件事**：在门户 → 订阅 → 策略里确认可选区域列表。
如果东亚不可用，延迟优势会大打折扣（可能落到美区 ⇒ 200ms+）。

学生订阅的服务范围与排除项本身没问题：Functions / ACA / App Service / VM 均**不在排除项**内
（[排除项原文](https://azure.microsoft.com/en-us/pricing/offers/ms-azr-0170p) 只排除 support plans、
Azure DevOps、ExpressRoute、Marketplace 第三方产品等）。$100 额度 12 个月有效
（[education-hub FAQ](https://learn.microsoft.com/en-us/azure/education-hub/faq)）。

---

## 5. 建议

1. **不要为这个服务选 Functions。** 架构不匹配（有状态 + CPU 密集 + 容器 + 长任务），
   三条官方明文（无状态要求 / 后台任务会被 site shutdown / HTTP 230 秒）逐条命中，不是配置能绕过的。
2. **首选方案 A**（与卫戍协议共用 VM）：零成本、单实例、状态问题天然消失、无超时无冷启动。
   等 10-11 学生认证解锁、VM 开好后一起部署。
3. **次选方案 B**（ACA）：仅当「能接受冷启动 + 愿意把状态外置」时才划算（否则要么多实例出错，
   要么锁单实例变常驻计费）。
4. **部署前必做**：① 确认真实 `docker build` 能起来（本机无 docker CLI，这是所有方案的共同前置风险）；
   ② 确认学生订阅的可用区域。
5. **数据不入镜像**这条底线继续遵守：`.blk` 是 War Thunder 数据（© Gaijin），
   `.gitignore` 已排除，运行期靠 `DATA_DIR` 挂载。

---

## 6. 遗留 / 未决

- **前端还没接后端**：`E:\导弹包线图-ghpages` 里目前**没有任何指向本服务的 `fetch`**
  （只有 `fetch("./catalog.json")` / `fetch("./notes.json")`），W3 尚未开始。
  ⇒ 部署的紧迫性不高，可以等前端接上再定后端形态。
- **AGPL-3.0-only**：二次分发/托管需注意许可证义务（本仓 `pyproject.toml` 已声明）。
- 本机**无 `docker` CLI** ⇒ 无法本地 build/run 验证镜像。
- 官方未公布 Python 冷启动秒数、Container Apps 定价页按区域动态渲染
  （单价已由零售价 API 补全，见 §3 方案 B 表格）。
