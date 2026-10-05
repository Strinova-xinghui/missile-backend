# -*- coding: utf-8 -*-
"""运行期配置：**全部来自环境变量**（默认值给本机开发用），外加一个可读的错误类型。

设计口径：
* 数据不在仓里 ⇒ 唯一入口是 `DATA_DIR`（`data_dir()` 显式读它，别用求解器包的默认值 —— 那会把
  "忘了设"伪装成"用了别处的数据"）。
* CORS 白名单**没有通配**：`CORS_ORIGINS` 是显式清单，本地开发额外允许 `http://127.0.0.1:<任意端口>`
  （由 `ALLOW_LOCALHOST_ORIGINS` 开关，默认开）。
* 限额全部可调：请求体上限（keys 数、网格节点数、`iso_step` 下限）与按 IP 的令牌桶。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

APP_VERSION = "0.1.0"
API_PREFIX = "/v1"

#: 本地开发的来源正则（**只**放回环地址，别写成 `*`）。
LOCALHOST_ORIGIN_REGEX = r"^http://(127\.0\.0\.1|localhost)(:\d{1,5})?$"


def _get(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _int(name: str, default: int, *, lo: int = 1, hi: int = 10**9) -> int:
    raw = _get(name)
    if not raw:
        return default
    try:
        return max(lo, min(hi, int(raw)))
    except ValueError:
        return default


def _float(name: str, default: float, *, lo: float = 1e-9, hi: float = 1e9) -> float:
    raw = _get(name)
    if not raw:
        return default
    try:
        return max(lo, min(hi, float(raw)))
    except ValueError:
        return default


def _flag(name: str, default: bool) -> bool:
    raw = _get(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# --------------------------------------------------------------------- 数据与身份
def data_dir() -> str:
    """`DATA_DIR` 原样返回（可能为空 ⇒ 调用点负责报错，见 `physics.require_data_dir`）。"""
    return os.environ.get("DATA_DIR", "").strip()


# --------------------------------------------------------------------- 求解
def tier_default() -> str:
    tier = _get("TIER_DEFAULT", "fast").lower()
    return tier if tier in ("standard", "fast") else "fast"


def standard_missile() -> str:
    """等时线用的**基准弹**（池内短名）。与主仓 `workflow` 的默认一致：PL-12。"""
    return _get("STANDARD_MISSILE", "PL-12")


# --------------------------------------------------------------------- 限额
def max_keys() -> int:
    return _int("MAX_KEYS", 168, lo=1, hi=168)


def min_iso_step() -> float:
    return _float("MIN_ISO_STEP", 0.5, lo=0.01, hi=600.0)


def sync_max_nodes() -> int:
    """同步请求的网格节点上限（超过 ⇒ 请走 `/v1/jobs`）。"""
    return _int("SYNC_MAX_NODES", 400, lo=4, hi=200_000)


def job_max_nodes() -> int:
    return _int("JOB_MAX_NODES", 20_000, lo=4, hi=2_000_000)


def rate_limit_per_min() -> int:
    return _int("RATE_LIMIT_PER_MIN", 60, lo=1, hi=100_000)


def rate_limit_burst() -> int:
    return _int("RATE_LIMIT_BURST", 12, lo=1, hi=10_000)


# --------------------------------------------------------------------- 任务
def job_workers() -> int:
    return _int("JOB_WORKERS", 2, lo=1, hi=32)


def job_ttl_s() -> float:
    return _float("JOB_TTL_S", 3600.0, lo=60.0, hi=86_400.0)


# --------------------------------------------------------------------- 缓存
def cache_dir() -> Path | None:
    raw = _get("CACHE_DIR")
    return Path(raw) if raw else None


def cache_max_items() -> int:
    return _int("CACHE_MAX_ITEMS", 32, lo=1, hi=4096)


def cache_enabled() -> bool:
    return _flag("CACHE_ENABLED", True)


# --------------------------------------------------------------------- CORS
def cors_origins() -> list[str]:
    """显式来源清单。默认只有站点 GitHUb Pages 那一个（绝不含 `*`）。"""
    raw = _get("CORS_ORIGINS", "https://strinova-xinghui.github.io")
    out = [o.strip() for o in re.split(r"[,\s]+", raw) if o.strip()]
    for o in out:
        if o == "*":
            raise ValueError("CORS_ORIGINS 不许用 `*`：白名单要写全（见 README「CORS」）")
    return out


def allow_localhost_origins() -> bool:
    return _flag("ALLOW_LOCALHOST_ORIGINS", True)


# --------------------------------------------------------------------- 错误
class Problem(Exception):
    """带 HTTP 状态码与机器可读 `code` 的可读错误（`main.py` 统一转成 JSON）。"""

    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status = int(status)
        self.code = str(code)
        self.message = str(message)
        self.extra = dict(extra)

    def payload(self) -> dict:
        out = {"ok": False, "error": {"code": self.code, "message": self.message}}
        if self.extra:
            out["error"]["details"] = self.extra
        return out


def bad_request(code: str, message: str, **extra) -> Problem:
    return Problem(400, code, message, **extra)


def too_many(message: str, **extra) -> Problem:
    return Problem(429, "rate_limited", message, **extra)


def unavailable(code: str, message: str, **extra) -> Problem:
    return Problem(503, code, message, **extra)
