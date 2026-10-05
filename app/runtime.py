# -*- coding: utf-8 -*-
"""请求侧的运行设施：**结果缓存 / 令牌桶限流 / 后台任务**（三块互不依赖，各自一节）。

* 缓存：键 = `physics.cache_key()`（与主仓 `cache_key()` 同口径）；进程内 LRU 为主，
  `CACHE_DIR` 给了就再落一份磁盘 JSON（重启后仍能命中 ⇒ `cached:true`）。
* 限流：按 IP 的**令牌桶**（`RATE_LIMIT_PER_MIN` 匀速回填 + `RATE_LIMIT_BURST` 突发），
  超限抛 `Problem(429)`；另有一层"单请求上限"在路由里查（keys 数、网格节点数、`iso_step` 下限）。
* 任务：`ThreadPoolExecutor` + 一个内存任务表（`running` / `done` / `error`，`progress` 单调），
  长扫描（全池/大网格）走这里，轻请求同步返回。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections import OrderedDict

from . import config


# --------------------------------------------------------------------- 结果缓存
class ResultCache:
    """进程内 LRU（+ 可选磁盘）。线程安全：命中/写回都在锁里，值是不可变 JSON 结构。"""

    def __init__(self, *, max_items: int | None = None, directory=None):
        self._max = int(max_items if max_items is not None else config.cache_max_items())
        self._dir = directory if directory is not None else config.cache_dir()
        self._lock = threading.Lock()
        self._mem: OrderedDict[str, dict] = OrderedDict()
        self._hits = 0
        self._misses = 0
        if self._dir is not None:
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
            except Exception:                                       # noqa: BLE001
                self._dir = None                                   # 盘写不了就当没有磁盘缓存

    # -- 统计 ---------------------------------------------------------------
    def stats(self) -> dict:
        return {"hits": self._hits, "misses": self._misses, "items": len(self._mem),
                "max_items": self._max, "disk": str(self._dir) if self._dir else None}

    # -- 读写 ---------------------------------------------------------------
    def get(self, key: str) -> dict | None:
        if not config.cache_enabled():
            return None
        with self._lock:
            got = self._mem.get(key)
            if got is not None:
                self._mem.move_to_end(key)
                self._hits += 1
                return got
        disk = self._disk_path(key)
        if disk is not None and disk.is_file():
            try:
                got = json.loads(disk.read_text(encoding="utf-8"))
            except Exception:                                       # noqa: BLE001
                got = None
            if isinstance(got, dict):
                with self._lock:
                    self._mem[key] = got
                    self._mem.move_to_end(key)
                    self._trim_locked()
                    self._hits += 1
                return got
        with self._lock:
            self._misses += 1
        return None

    def put(self, key: str, value: dict) -> None:
        if not config.cache_enabled():
            return
        with self._lock:
            self._mem[key] = value
            self._mem.move_to_end(key)
            self._trim_locked()
        disk = self._disk_path(key)
        if disk is not None:
            try:
                tmp = disk.with_suffix(".tmp")
                tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
                tmp.replace(disk)
            except Exception:                                       # noqa: BLE001
                pass

    def clear(self) -> None:
        with self._lock:
            self._mem.clear()

    # -- 内部 ---------------------------------------------------------------
    def _trim_locked(self) -> None:
        while len(self._mem) > self._max:
            self._mem.popitem(last=False)

    def _disk_path(self, key: str):
        if self._dir is None or not config.cache_enabled():
            return None
        return self._dir / f"{key}.json"


# --------------------------------------------------------------------- 限流
class TokenBucket:
    """按 key（这里用客户端 IP）的令牌桶：`rate` 个/分钟匀速回填，最多 `burst` 个。"""

    def __init__(self, *, per_min: int | None = None, burst: int | None = None):
        self.rate = float(per_min if per_min is not None else config.rate_limit_per_min()) / 60.0
        self.burst = float(burst if burst is not None else config.rate_limit_burst())
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}          # key -> (tokens, last_ts)

    def take(self, key: str, *, cost: float = 1.0) -> tuple[bool, float]:
        """返回 `(是否放行, 需要等多少秒)`；不放行时桶不动。"""
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(key, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            if tokens >= cost:
                self._buckets[key] = (tokens - cost, now)
                return True, 0.0
            need = (cost - tokens) / self.rate if self.rate > 0 else 60.0
            self._buckets[key] = (tokens, now)
            return False, need

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


# --------------------------------------------------------------------- 后台任务
class JobStore:
    """内存任务表：`running` → `done` / `error`，`progress` 只增不减。"""

    def __init__(self, *, workers: int | None = None, ttl_s: float | None = None):
        from concurrent.futures import ThreadPoolExecutor

        self._pool = ThreadPoolExecutor(max_workers=int(workers or config.job_workers()),
                                        thread_name_prefix="mb-job")
        self._ttl = float(ttl_s or config.job_ttl_s())
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}

    def submit(self, fn, *, label: str = "") -> str:
        job_id = uuid.uuid4().hex[:16]
        with self._lock:
            self._jobs[job_id] = {"job_id": job_id, "state": "running", "progress": 0.0,
                                  "label": label, "result": None, "error": None,
                                  "created": time.time()}
        self._gc()

        def wrapper():
            def report(frac: float, note: str = "") -> None:
                self._progress(job_id, frac, note)
            try:
                res = fn(report)
            except config.Problem as exc:
                self._fail(job_id, exc.message)
            except Exception as exc:                                # noqa: BLE001
                self._fail(job_id, f"{type(exc).__name__}: {exc}")
            else:
                self._done(job_id, res)

        self._pool.submit(wrapper)
        return job_id

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return None if job is None else dict(job)

    def count(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if j["state"] == "running")

    # -- 内部 ---------------------------------------------------------------
    def _progress(self, job_id: str, frac: float, note: str = "") -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            frac = max(0.0, min(1.0, float(frac)))
            job["progress"] = max(float(job["progress"]), frac)     # 单调
            if note:
                job["note"] = str(note)

    def _done(self, job_id: str, result) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job["state"] = "done"
            job["progress"] = 1.0
            job["result"] = result
            job["finished"] = time.time()

    def _fail(self, job_id: str, message: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job["state"] = "error"
            job["error"] = str(message)
            job["finished"] = time.time()

    def _gc(self) -> None:
        now = time.time()
        with self._lock:
            dead = [k for k, j in self._jobs.items()
                    if j.get("finished") and now - float(j["finished"]) > self._ttl]
            for k in dead:
                self._jobs.pop(k, None)
