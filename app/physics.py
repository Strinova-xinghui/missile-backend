# -*- coding: utf-8 -*-
"""与求解器包的接线层：**身份、数据门、轴口径、等值线口径、单次解算**。

三条设计口径（与 README 的"口径"一节对应）：

1. **物理只在求解器包里**：本模块不写任何物理公式，只负责"把请求翻译成 `solver.run()` 的入参"
   （`mapping.scaling_for()` 做 ΔV/β 的缩放，与主仓 `workflow` 用的是同一个函数）。
2. **轴与等值线的口径对齐主仓**：主仓 `figures.adaptive_axis()` / `figures.bg_axis()` 的**范围**公式、
   `grid.make_grid()` 的**向内对齐**、`grid.iso_levels()` 的**整步长层级**、`grid.extract_isolines()`
   的**线性等值线**，这里各有一份**等价实现**（标了"口径对齐 main@<sha>"）——因为公开发布的
   `missile-solver` 包里没有 `figures`/`grid`（它只管求解，不出图）。数值判据见 `tests/test_overlay.py`。
3. **数据门在启动时**：`verify_data(DATA_DIR)` 不通过 ⇒ 抛错 ⇒ 进程退出（不带着错数据服务）。
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, is_dataclass, replace

from . import config

#: 轴与等值线的口径来源（主仓 —— 只在 README/注释里标出处，代码不依赖它）。
MAIN_REPO_ISO_REF = "main repo grid.make_grid/iso_levels/extract_isolines + figures.adaptive_axis/bg_axis"

PAD_FRAC = 0.06                      # 与 figures.PAD_FRAC 同值
MIN_PAD = {"x": 12.0, "y": 60.0}     # 与 figures.MIN_PAD 同值（图1 的 ΔV / BC）
BG_MIN_PAD = {"x": 60.0, "y": 9e-4}  # 与 figures.bg_axis 的两个下限同值（β / ginv）


# --------------------------------------------------------------------- 求解器接线
def _pkg():
    """惰性 import 求解器包（缺包时给一条能读懂的错误，而不是 ImportError 堆栈）。"""
    try:
        import missile_solver
    except Exception as exc:                                        # noqa: BLE001
        raise config.unavailable(
            "solver_missing",
            "没装求解器包 missile-solver（pip install -r requirements.txt）",
            detail=f"{type(exc).__name__}: {exc}") from exc
    return missile_solver


def require_data_dir() -> str:
    d = config.data_dir()
    if not d:
        raise RuntimeError(
            "没有设置 DATA_DIR：本服务**不带数据**（见 README「数据」一节）。"
            r"本机开发可指向 E:\导弹包线图-release\vendor\wt-missile\inputs\resources\2.59.0.28")
    return d


def data_gate() -> dict:
    """启动时的**数据门**：返回身份信息；不通过就抛 `RuntimeError`（进程退出）。"""
    pkg = _pkg()
    directory = require_data_dir()
    try:
        ok, notes = pkg.verify_data(directory)
    except Exception as exc:                                        # noqa: BLE001
        raise RuntimeError(f"数据校验失败：{type(exc).__name__}: {exc}") from exc
    if not ok:
        raise RuntimeError("数据校验不通过（拒绝启动）：\n  - " + "\n  - ".join(str(n) for n in notes))
    return {"dir": directory, "ok": True, "notes": [str(n) for n in notes],
            "identity": data_identity()}


def data_identity() -> dict:
    """数据身份：资源版本 + 求解器包钉的哈希表整体指纹（不逐个文件列，量大且没必要）。"""
    pkg = _pkg()
    ident = solver_identity()
    return {"version": ident.get("version"), "resource_sha256": _resource_fingerprint(pkg),
            "dir": config.data_dir()}


def _resource_fingerprint(pkg) -> str:
    """`checksums.json`（或有则用的哈希表）的 sha256 —— "手上这份数据是哪一版"的稳定指纹。"""
    try:
        path = pkg._data.checksums_path()
    except Exception:                                               # noqa: BLE001
        return ""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:                                               # noqa: BLE001
        return ""


def solver_identity() -> dict:
    pkg = _pkg()
    ident = dict(pkg.solver.identity() or {})
    return {k: ident.get(k) for k in ("solver", "model", "version", "sha256",
                                      "archive_sha256", "n_profiles")}


def catalog_record() -> dict:
    """与站点 `catalog.json` 同 schema 的目录（**唯一出处是求解器包**，这里不另写一份）。

    ⚠ 站点那份是"落盘目录 + webui 补的 style/standard/presets"；本服务只出**求解器包原样的记录**
    （含 `missiles`/`unsupported`/`reference`/`solver`/`count`/`computable`），前端要的
    `style`/`presets` 属于页面口径，不在本契约里（README 有说明）。
    """
    pkg = _pkg()
    return pkg.catalog.build()


def presets() -> list:
    """弹池那一排"预设按钮"的数据：**规则不在本仓** —— 直接问求解器包的 `pool`（`in_pool11` 那批）。

    与站点 `webui._catalog_presets()` 同规则同值（判据：`tests/test_api.py::test_catalog_presets_match_site`，
    站点那份存在时逐值比对 ⇒ "两处规则"钉成"一处事实"）。将来加「仅红外弹」时两边一起长。
    """
    pkg = _pkg()
    keys = sorted(str(p.key) for p in pkg.pool.points(None))
    return [{"label": "仅主动弹", "keys": keys,
             "hint": f"目录里标了 in_pool11 的那 {len(keys)} 型主动雷达弹（默认弹池）"}]


def pool_rows(keys=None) -> list:
    """弹池行（短名 → (ΔV, BC, native)），给等时线当基准弹用。"""
    pkg = _pkg()
    rows = []
    for p in pkg.pool.points(keys):
        d = asdict(p) if is_dataclass(p) else dict(p)
        rows.append({k: d.get(k) for k in ("key", "label", "native", "dv", "bc")})
    return rows


def metrics_of(key: str):
    pkg = _pkg()
    return pkg.pool.metrics_of(pkg.pool.resolve(key))


def run_case_bg(beta: float, ginv: float, *, dv: float, native: str, metrics, point,
                tier: str, scene=None):
    """bg 平面的一跑：**照抄主仓 `workflow.overlay_bg()` 的通路** ——

    `missile_solver.bg.scaling_for(point, metrics, beta=β_t, ginv=ginv_t, dv=ΔV_pin)`：
    β 走 `mapping.scaling_for` 的 `bc_target` 位置（β ≡ BC），ginv 由 `gamma_of()` 落回物理 γ
    再算 `cxaoa_scale`（唯一的 γ/ginv 换算点在包里）。**本模块不自己算倍率**。
    """
    pkg = _pkg()
    scene = pkg.Scene() if scene is None else scene
    scaling = pkg.bg.scaling_for(point, metrics, beta=float(beta), ginv=float(ginv), dv=float(dv))
    return pkg.solver.run(scene, missile=native, scaling=scaling, tier=tier, want_cpa=False)


def run_case(dv_target: float, bc_target: float, *, native: str, metrics, tier: str,
             scene=None, cxaoa_scale: float = 1.0):
    """一次解算：把目标 (ΔV, β) 通过 `mapping.scaling_for()` 施加到基准弹上。

    `cxaoa_scale` 只在等时面（要独立扫 γ）时用；等时线路径恒为 1.0（与主仓同口径）。
    """
    pkg = _pkg()
    scene = pkg.Scene() if scene is None else scene
    scaling = pkg.mapping.scaling_for(float(dv_target), float(bc_target), metrics)
    if abs(float(cxaoa_scale) - 1.0) > 1e-12:
        scaling = replace(scaling, cxaoa_scale=float(cxaoa_scale))
    return pkg.solver.run(scene, missile=native, scaling=scaling, tier=tier, want_cpa=False)


# --------------------------------------------------------------------- 轴（口径对齐主仓）
def _pad_axis(vals, *, frac: float, min_pad: float) -> tuple:
    lo, hi = float(min(vals)), float(max(vals))
    pad = max((hi - lo) * frac, float(min_pad))
    return (round(lo - pad, 6), round(hi + pad, 6))


def axis_plane(rows) -> dict:
    """图1 的轴域：`xlim` = ΔV、`ylim` = β（口径对齐 `figures.adaptive_axis`）。

    ⚠ 主仓那条轴**落定到 3 位小数**（`AxisSpec(xlim=(round(x0, 3), round(x1, 3)))`）—— 金标夹具
    第一枪就是这里对不上：适配层算出 `920.569749`，主仓是 `920.57`。这是"对齐主仓"，
    **不是**放宽容差。（`bg` 那条轴主仓不落位小数 ⇒ 见 `axis_bg`，保持原样。）
    """
    if not rows:
        raise config.bad_request("empty_keys", "轴至少需要一个弹")
    return {"xlim": list(_round_to(_pad_axis([r["dv"] for r in rows], frac=PAD_FRAC,
                                            min_pad=MIN_PAD["x"]), 3)),
            "ylim": list(_round_to(_pad_axis([r["bc"] for r in rows], frac=PAD_FRAC,
                                            min_pad=MIN_PAD["y"]), 3))}


def _round_to(pair, digits: int) -> tuple:
    return (round(float(pair[0]), digits), round(float(pair[1]), digits))


def axis_bg(rows) -> dict:
    """图2 的轴域：`xlim` = β、`ylim` = ginv（口径对齐 `figures.bg_axis`）。"""
    if not rows:
        raise config.bad_request("empty_keys", "轴至少需要一个弹")
    return {"xlim": list(_pad_axis([r["bc"] for r in rows], frac=0.06, min_pad=BG_MIN_PAD["x"])),
            "ylim": list(_pad_axis([r["ginv"] for r in rows], frac=0.06, min_pad=BG_MIN_PAD["y"]))}


def arange_inclusive(lo: float, hi: float, step: float) -> tuple:
    """闭区间等差（含端点补足）—— 与主仓 `grid.arange_inclusive` 同口径。"""
    if step <= 0:
        raise config.bad_request("bad_step", "步长必须为正")
    n = int(math.floor((float(hi) - float(lo)) / float(step) + 1e-9)) + 1
    vals = [float(lo) + float(step) * i for i in range(max(n, 1))]
    if vals[-1] < float(hi) - 1e-9:
        vals.append(float(hi))
    return tuple(round(v, 10) for v in vals)


def make_grid(xlim, ylim, *, step: float, step_y: float) -> tuple:
    """轴范围 → 扫描网格（范围按步长**向内对齐**）—— 与主仓 `grid.make_grid` 同口径。"""
    lo_x, hi_x = float(xlim[0]), float(xlim[1])
    lo_y, hi_y = float(ylim[0]), float(ylim[1])
    x0 = math.ceil(lo_x / step - 1e-9) * step
    y0 = math.ceil(lo_y / step_y - 1e-9) * step_y
    return arange_inclusive(x0, hi_x, step), arange_inclusive(y0, hi_y, step_y)


def iso_levels(values, step: float) -> list:
    """等时线层级：只取落在数据范围内的整步长值 —— 与主仓 `grid.iso_levels` 同口径。"""
    finite = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if not finite:
        return []
    lo, hi = min(finite), max(finite)
    first = math.ceil((lo - 1e-9) / float(step)) * float(step)
    out, x = [], first
    while x <= hi + 1e-9:
        out.append(round(x, 6))
        x += float(step)
    return out


def marching_squares(xs, ys, grid, levels) -> list:
    """线性等值线（口径对齐主仓 `grid.extract_isolines`，实现不依赖 matplotlib）。

    `grid[i][j]` = 格点 `(xs[i], ys[j])` 的值；`nan` 表示未命中（**不参与**插值）。
    返回 `[{"level": t, "points": [[x, y], …], "closed": bool}, …]`，其中 `points` 是**首尾相接的折线**
    （与主仓 `contour.allsegs` 同一形态）。

    ⚠ 2026-10-05 金标夹具第二枪：最初把每个格子的两个交点**直接拼在一起**，导致相邻格子共享的那个交点
    出现两次（实测 8 点 vs 主仓 5 点）⇒ 这里改成先收"格子 → 一段（两点）"，再按端点**串成折线**。
    """
    seg_out = []
    for level in levels:
        segs = []
        for i in range(len(xs) - 1):
            for j in range(len(ys) - 1):
                corners = ((xs[i], ys[j], grid[i][j]), (xs[i + 1], ys[j], grid[i + 1][j]),
                           (xs[i + 1], ys[j + 1], grid[i + 1][j + 1]),
                           (xs[i], ys[j + 1], grid[i][j + 1]))
                seg = _cell_segment(corners, float(level))
                if seg:
                    segs.append(tuple(seg))
        if not segs:
            continue
        pts, closed = [], False
        for line in _chain(segs):
            pts.extend(line)
            if len(line) >= 3 and _same(line[0], line[-1]):
                closed = True
        if pts:
            seg_out.append({"level": float(level), "points": pts, "closed": closed})
    return seg_out


def _chain(segs) -> list:
    """把"一段两点"的集合串成折线：贪心接端点（容差 `1e-9`），接不上就开新折线。"""
    todo = list(segs)
    lines = []
    while todo:
        line = list(todo.pop(0))
        grew = True
        while grew:
            grew = False
            for k, seg in enumerate(todo):
                a, b = seg
                if _same(line[-1], a):
                    line.append(b)
                elif _same(line[-1], b):
                    line.append(a)
                elif _same(line[0], b):
                    line.insert(0, a)
                elif _same(line[0], a):
                    line.insert(0, b)
                else:
                    continue
                todo.pop(k)
                grew = True
                break
        # 闭合的最后一点与首点重合时，去掉重复的尾点（与 allsegs 一致）
        if len(line) >= 3 and _same(line[0], line[-1]):
            line = line[:-1]
        lines.append(line)
    return lines


def _same(a, b, tol: float = 1e-9) -> bool:
    return abs(a[0] - b[0]) < tol and abs(a[1] - b[1]) < tol


def _cell_segment(corners, level):
    """一个格子的一段等值线（线性插值）；没有穿越/有 `nan` ⇒ None。"""
    pts = []
    for k in range(4):
        x0, y0, v0 = corners[k]
        x1, y1, v1 = corners[(k + 1) % 4]
        if v0 is None or v1 is None or not (math.isfinite(v0) and math.isfinite(v1)):
            continue
        if (v0 - level) * (v1 - level) < 0:
            f = (level - v0) / (v1 - v0)
            pts.append((round(x0 + (x1 - x0) * f, 9), round(y0 + (y1 - y0) * f, 9)))
        elif v0 == level:          # 顶点正好落在层级上：只取一次，避免重复点
            pts.append((round(x0, 9), round(y0, 9)))
    if len(pts) < 2:
        return None
    # 一个格子最多两个交点（线性场）；>2 是顶点命中造成的重复 ⇒ 去重后取前两个
    uniq = []
    for p in pts:
        if not any(_same(p, q) for q in uniq):
            uniq.append(p)
    return uniq[:2] if len(uniq) >= 2 else None


# --------------------------------------------------------------------- 结果块 / 缓存键
def identity_block(*, tier: str, backend: str = "", cached: bool = False,
                   elapsed_ms: float = 0.0, nodes: int = 0, extra: dict | None = None) -> dict:
    si, di = solver_identity(), data_identity()
    out = {"solver_sha256": si.get("sha256"), "resource_sha256": di.get("resource_sha256"),
           "solver": si, "tier": tier, "backend": backend or "unreported",
           "cached": bool(cached), "elapsed_ms": round(float(elapsed_ms), 1), "nodes": int(nodes)}
    if extra:
        out.update(extra)
    return out


def cache_key(parts: dict) -> str:
    """与主仓 `cache_key()` **同口径**：把"输入 → 结果"的全部要素做成规范化 JSON 再 sha256。

    主仓那边吃的是 `Scene` + `Scaling` + tier + schema；这里对应的是"场景（默认）+ 基准弹 +
    目标 (ΔV, β) + tier + 目标版本哈希"，够用来判"这次请求是否与缓存里那份逐要素相同"。
    """
    payload = dict(parts)
    payload["solver_sha256"] = solver_identity().get("sha256")
    payload["resource_sha256"] = data_identity().get("resource_sha256")
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
