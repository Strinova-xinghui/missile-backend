# -*- coding: utf-8 -*-
"""HTTP 层：路由、CORS、限流、错误形状。**计算在 `overlay.py`，配置在 `config.py`。**

启动顺序（很重要）：`lifespan` 里先跑 `physics.data_gate()` —— `DATA_DIR` 没设、缺件或哈希不符
⇒ 直接抛错 ⇒ **进程退出**（不带着错数据对外服务）。健康检查里的 `data.dir_ok` 就是这次校验的结果。

选 FastAPI 的理由（README「为什么 FastAPI」也写了）：契约是纯 JSON 进出，需要**声明式**的请求校验
（keys 数 / 步长 / 档位都在模型上）+ 自动 schema（`/openapi.json`、`/docs` 给 W2/W3 对齐）+
线程池卸载（解算是 CPU 密集，不能堵事件循环）。
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import APP_VERSION, config, overlay, physics
from .runtime import JobStore, ResultCache, TokenBucket

#: 进程级单例（缓存 / 限流 / 任务表）—— 单进程部署够用；多副本时缓存各自独立（README 有说明）。
CACHE = ResultCache()
BUCKET = TokenBucket()
JOBS = JobStore()
#: 启动时数据门的结果（health 用它报 `dir_ok` / `identity`）。
DATA_STATE: dict = {}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global DATA_STATE
    DATA_STATE = physics.data_gate()          # 不通过 ⇒ 抛错 ⇒ uvicorn 退出
    yield


app = FastAPI(title="missile-backend", version=APP_VERSION,
              description="War Thunder 主动弹：等时线（overlay）与等时面（isosurface）计算服务",
              lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.cors_origins(),                    # 显式白名单，**没有** `*`
    allow_origin_regex=config.LOCALHOST_ORIGIN_REGEX if config.allow_localhost_origins() else None,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=False,
    max_age=600,
)


# --------------------------------------------------------------------- 错误形状
@app.exception_handler(config.Problem)
async def _problem_handler(_req: Request, exc: config.Problem):
    return JSONResponse(status_code=exc.status, content=exc.payload())


@app.exception_handler(RequestValidationError)
async def _validation_handler(_req: Request, exc: RequestValidationError):
    first = (exc.errors() or [{}])[0]
    loc = ".".join(str(x) for x in first.get("loc", ()) if x != "body")
    problem = config.bad_request("bad_request",
                                 f"请求体不合法：{loc or 'body'} {first.get('msg', '')}".strip())
    return JSONResponse(status_code=400, content=problem.payload())


@app.exception_handler(Exception)
async def _unhandled(_req: Request, exc: Exception):        # pragma: no cover - 兜底
    return JSONResponse(status_code=500, content={
        "ok": False, "error": {"code": "internal", "message": f"{type(exc).__name__}: {exc}"}})


# --------------------------------------------------------------------- 限流中间件
@app.middleware("http")
async def _rate_limit(request: Request, call_next):
    path = request.url.path
    if path.startswith(config.API_PREFIX) and path not in (f"{config.API_PREFIX}/health",):
        who = (request.client.host if request.client else "unknown")
        ok, need = BUCKET.take(who)
        if not ok:
            problem = config.too_many(
                f"请求太频繁：每分钟 {config.rate_limit_per_min()} 次（突发 {config.rate_limit_burst()}）"
                f"，请等 {need:.1f} s 再试", retry_after_s=round(need, 1))
            return JSONResponse(status_code=429, content=problem.payload(),
                                headers={"Retry-After": str(max(1, int(need + 0.5)))})
    return await call_next(request)


# --------------------------------------------------------------------- 请求模型
class OverlayReq(BaseModel):
    kind: Literal["plane", "bg"]
    keys: list[str] | None = None
    tier: Literal["standard", "fast"] | None = None
    dv_pin: float | None = Field(default=None, description="bg 的固定 ΔV；不给取基准弹标称值")
    iso_step: float = Field(default=1.0, description="等时线层级间隔 [s]")
    dv_step: float | None = Field(default=None, description="plane 的 ΔV 步长（默认 10）")
    bc_step: float | None = Field(default=None, description="plane 的 β 步长（默认 25）")
    beta_step: float | None = Field(default=None, description="bg 的 β 步长（默认 30）")
    ginv_step: float | None = Field(default=None, description="bg 的 ginv 步长（默认 4e-4）")
    standard: str | None = Field(default=None, description="基准弹（池内短名），默认 PL-12")
    sync: bool = Field(default=True, description="false ⇒ 允许用 job 上限的节点数（仅供 /v1/jobs 内部）")


class IsoReq(BaseModel):
    keys: list[str] = Field(description="等时面一次只算一枚")
    tier: Literal["standard", "fast"] | None = None
    level_s: float = Field(default=1.0, description="等值面的 t_hit 层级 [s]")
    grid: int | None = Field(default=None, description="每轴采样数（默认 5 ⇒ 125 个顶点）")
    sync: bool = True


class JobReq(BaseModel):
    op: Literal["overlay", "isosurface"] | None = None
    kind: Literal["plane", "bg", "fig3"] | None = None
    keys: list[str] | None = None
    tier: Literal["standard", "fast"] | None = None
    dv_pin: float | None = None
    iso_step: float = 1.0
    dv_step: float | None = None
    bc_step: float | None = None
    beta_step: float | None = None
    ginv_step: float | None = None
    standard: str | None = None
    level_s: float = 1.0
    grid: int | None = None


# --------------------------------------------------------------------- 路由
@app.get("/")
async def root():
    return {"ok": True, "service": "missile-backend", "version": APP_VERSION,
            "docs": "/docs", "openapi": "/openapi.json", "api": config.API_PREFIX}


@app.get(f"{config.API_PREFIX}/health")
async def health():
    si = physics.solver_identity()
    di = physics.data_identity()
    return {
        "ok": True,
        "version": APP_VERSION,
        "solver": {"model": si.get("model"), "version": si.get("version"), "sha256": si.get("sha256")},
        "data": {"dir_ok": bool(DATA_STATE.get("ok")), "identity": di,
                 "notes": list(DATA_STATE.get("notes") or [])},
        "tier_default": config.tier_default(),
        "limits": {"max_keys": config.max_keys(), "min_iso_step": config.min_iso_step(),
                   "sync_max_nodes": config.sync_max_nodes(), "rate_limit_per_min": config.rate_limit_per_min()},
        "cache": CACHE.stats() | {"running_jobs": JOBS.count()},
    }


@app.get(f"{config.API_PREFIX}/catalog")
async def catalog():
    rec = physics.catalog_record()
    # `presets` 与站点同规则：由求解器包的 pool（`in_pool11`）现算 —— 规则只有一处
    return {"ok": True, **rec, "presets": physics.presets()}


@app.post(f"{config.API_PREFIX}/overlay")
async def post_overlay(req: OverlayReq):
    _check_keys(req.keys)
    return overlay.overlay(req.kind, req.keys, tier=req.tier, dv_pin=req.dv_pin,
                           iso_step=req.iso_step, dv_step=req.dv_step, bc_step=req.bc_step,
                           beta_step=req.beta_step, ginv_step=req.ginv_step,
                           standard=req.standard, sync=req.sync, cache=CACHE)


@app.post(f"{config.API_PREFIX}/isosurface")
async def post_isosurface(req: IsoReq):
    _check_keys(req.keys)
    return overlay.isosurface(req.keys, tier=req.tier, level_s=req.level_s, grid=req.grid,
                              sync=req.sync, cache=CACHE)


@app.post(f"{config.API_PREFIX}/jobs")
async def post_jobs(req: JobReq):
    _check_keys(req.keys)
    op = req.op or ("isosurface" if (req.kind in (None, "fig3") and req.keys and len(req.keys) == 1)
                    and req.level_s else "overlay")
    if op == "isosurface":
        fn = lambda report: overlay.isosurface(req.keys, tier=req.tier, level_s=req.level_s,
                                               grid=req.grid, sync=False, report=report, cache=CACHE)
    else:
        kind = req.kind if req.kind in ("plane", "bg") else "plane"
        fn = lambda report: overlay.overlay(kind, req.keys, tier=req.tier, dv_pin=req.dv_pin,
                                            iso_step=req.iso_step, dv_step=req.dv_step,
                                            bc_step=req.bc_step, beta_step=req.beta_step,
                                            ginv_step=req.ginv_step, standard=req.standard,
                                            sync=False, report=report, cache=CACHE)
    job_id = JOBS.submit(fn, label=op)
    return {"ok": True, "job_id": job_id, "op": op,
            "poll": f"{config.API_PREFIX}/jobs/{job_id}"}


@app.get(f"{config.API_PREFIX}/jobs/{{job_id}}")
async def get_job(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise config.Problem(404, "job_not_found", f"没有这个任务：{job_id}")
    out = {"ok": True, "job_id": job_id, "state": job["state"], "progress": round(job["progress"], 4)}
    if job.get("note"):
        out["note"] = job["note"]
    if job["state"] == "done":
        out["result"] = job["result"]
    if job["state"] == "error":
        out["error"] = job["error"]
    if job.get("created"):
        out["age_s"] = round(time.time() - float(job["created"]), 1)
    return out


@app.get(f"{config.API_PREFIX}/estimate")
async def estimate(kind: Literal["plane", "bg"], keys: str | None = None,
                   dv_step: float | None = None, bc_step: float | None = None,
                   beta_step: float | None = None, ginv_step: float | None = None):
    """成本预估（不跑解算）：这次请求会铺多大网格、能不能走同步。"""
    ks = [k.strip() for k in (keys or "").split(",") if k.strip()] or None
    return {"ok": True, **overlay.grid_estimate(kind, ks, dv_step=dv_step, bc_step=bc_step,
                                                beta_step=beta_step, ginv_step=ginv_step)}


# --------------------------------------------------------------------- 小工具
def _check_keys(keys) -> None:
    if keys is not None and len(keys) > config.max_keys():
        raise config.bad_request("too_many_keys",
                                 f"keys 最多 {config.max_keys()} 个（收到 {len(keys)}）")
