# -*- coding: utf-8 -*-
"""等时线（`/v1/overlay`）与等时面（`/v1/isosurface`）的**计算**。

物理一律来自 `missile_solver`（`physics.run_case`）；这里只做四件事：
① 由弹池算轴域；② 铺扫描网格；③ 逐格点解算并填 `t_hit` 矩阵；④ 提等值线/等值面。

⚠ 与 W2 的关系：主仓的 `workflow.overlay_plane/overlay_bg`（以及 `--overlay … --json`）**尚未落地**
（本模块写于其之前）⇒ 这里是**同形状的适配层**，口径对齐主仓：
* 网格：`grid.make_grid()`（向内对齐）；步长默认 10 / 25，超节点上限时按 2 倍逐级粗化；
* 层级：`grid.iso_levels()`（整 `iso_step`，落在数据范围内）；
* 等值线：线性等值线（主仓走 matplotlib 的 `ax.contour`，两者在同一条线性场上给出**同一批交点**，
  顺序/分段不保证一致 ⇒ 判据按"点集合 + 容差"比，见 `tests/test_overlay.py`）。
等 W2 的契约落地后，本模块只需把 ③ 换成"转发/包一层"，轴与层级口径保持不变。
"""

from __future__ import annotations

import math
import time

from . import config, physics

#: 主仓 `workflow.PlaneRequest` 的默认步长（口径）：ΔV 10 / β 25。
DEFAULT_DV_STEP = 10.0
DEFAULT_BC_STEP = 25.0
#: 主仓 `workflow.BgRequest` 的默认步长（口径）：β 30 / **ginv 4e-4**。
#: ⚠ 2026-10-05 教训（金标夹具第一枪就打出来的漂移）：适配层最初把 plane 的 10/25 复用到 bg 上，
#: 而 bg 的纵轴是 ginv ≈ 0.015（1e-4 量级）⇒ 一步跨完整个轴、网格只剩一列，等时线全错。
#: 两种 kind 的步长**必须分开**，别图省事复用。
DEFAULT_BETA_STEP = 30.0
DEFAULT_GINV_STEP = 4e-4
#: 等时面的默认每轴采样数（5 ⇒ 125 个顶点；要更细就走 `/v1/jobs`）。
DEFAULT_CUBE_N = 5


def _coarsen(step: float, factor: int) -> float:
    return float(step) * (2 ** int(factor))


def grid_steps(kind: str, axis: dict, *, dv_step=None, bc_step=None, beta_step=None,
               ginv_step=None, max_nodes: int) -> tuple[float, float, int]:
    """定这次扫描的步长（两种 kind 各一套默认值，见上面那三个常量）：显式给了就用
    （受节点上限约束），否则从主仓默认值逐级粗化到上限内。"""
    if kind == "plane":
        x_step = float(dv_step) if dv_step else DEFAULT_DV_STEP
        y_step = float(bc_step) if bc_step else DEFAULT_BC_STEP
        explicit = bool(dv_step or bc_step)
    else:
        x_step = float(beta_step) if beta_step else DEFAULT_BETA_STEP
        y_step = float(ginv_step) if ginv_step else DEFAULT_GINV_STEP
        explicit = bool(beta_step or ginv_step)
    if x_step <= 0 or y_step <= 0:
        raise config.bad_request("bad_step", "步长必须为正")
    span_x = float(axis["xlim"][1]) - float(axis["xlim"][0])
    span_y = float(axis["ylim"][1]) - float(axis["ylim"][0])
    for _ in range(24):
        nx = int(span_x / x_step) + 2
        ny = int(span_y / y_step) + 2
        if nx * ny <= max_nodes:
            return x_step, y_step, nx * ny
        if explicit:                           # 显式步长不许偷偷改 ⇒ 超限直接报错
            raise config.Problem(413, "grid_too_large",
                                 f"网格太大（约 {nx}×{ny} = {nx * ny} 个格点，上限 {max_nodes}）"
                                 "—— 请放宽步长，或改用 /v1/jobs", nodes=nx * ny, limit=max_nodes)
        x_step, y_step = _coarsen(x_step, 1), _coarsen(y_step, 1)
    raise config.Problem(413, "grid_too_large", "这个轴域下粗化不到上限以内")


def _axis_rows(kind: str, keys):
    """轴与扫描用的行：`plane` 用 (ΔV, β)；`bg` 用 (β, ginv)（口径与站点一致，取目录里的 `bc` 列）。"""
    rec = physics.catalog_record()
    want = {str(k) for k in keys} if keys else None
    rows = []
    for m in rec.get("missiles") or []:
        if want is not None and str(m.get("key")) not in want:
            continue
        rows.append({"key": m.get("key"), "label": m.get("label"), "native": m.get("native"),
                     "dv": float(m.get("dv")), "bc": float(m.get("bc")),
                     "ginv": float(m.get("ginv")), "gamma": float(m.get("gamma"))})
    if not rows:
        raise config.bad_request("empty_keys", "这批 keys 在目录里一个都没命中（见 /v1/catalog）")
    if kind == "plane":
        return rows, physics.axis_plane(rows)
    return rows, physics.axis_bg(rows)


def overlay(kind: str, keys, *, tier: str | None = None, dv_pin: float | None = None,
            iso_step: float = 1.0, dv_step=None, bc_step=None, beta_step=None, ginv_step=None,
            standard: str | None = None, sync: bool = True, report=None, cache=None) -> dict:
    """等时线：`kind="plane"` 在 (ΔV, β) 上扫，`kind="bg"` 在 (β, ginv) 上扫。

    基准弹（等时线属于它）默认 `STANDARD_MISSILE`（PL-12）；`bg` 的固定 ΔV 默认取基准弹的标称值。
    """
    if kind not in ("plane", "bg"):
        raise config.bad_request("bad_kind", f'kind 只能是 "plane" 或 "bg"，收到 {kind!r}')
    tier = (tier or config.tier_default()).lower()
    if tier not in ("standard", "fast"):
        raise config.bad_request("bad_tier", f"tier 只能是 standard / fast，收到 {tier!r}")
    iso_step = float(iso_step)
    if iso_step < config.min_iso_step():
        raise config.bad_request("iso_step_too_small",
                                 f"iso_step 太小（{iso_step:g}），下限 {config.min_iso_step():g} s")
    keys = [str(k) for k in (keys or [])][: config.max_keys()] or None
    rows, axis = _axis_rows(kind, keys)
    std_key = str(standard or config.standard_missile())
    std_native, std_metrics, std_nominal = _standard(std_key)
    if kind == "bg" and dv_pin is None:
        dv_pin = float(std_nominal["dv"])
    limit = config.sync_max_nodes() if sync else config.job_max_nodes()
    x_step, y_step, nodes = grid_steps(kind, axis, dv_step=dv_step, bc_step=bc_step,
                                       beta_step=beta_step, ginv_step=ginv_step, max_nodes=limit)
    xs, ys = physics.make_grid(axis["xlim"], axis["ylim"], step=x_step, step_y=y_step)
    key = physics.cache_key({"op": "overlay", "kind": kind, "keys": keys, "standard": std_key,
                             "tier": tier, "iso_step": iso_step, "dv_step": x_step,
                             "bc_step": y_step, "beta_step": beta_step, "ginv_step": ginv_step,
                             "dv_pin": dv_pin,
                             "xlim": axis["xlim"], "ylim": axis["ylim"]})
    if cache is not None:
        hit = cache.get(key)
        if hit is not None:
            out = dict(hit)
            out["identity"] = dict(out.get("identity") or {})
            out["identity"]["cached"] = True
            out["identity"]["elapsed_ms"] = 0.0
            return out
    t0 = time.perf_counter()
    grid = []
    total = len(xs) * len(ys)
    backend = ""
    done = 0
    for i, xv in enumerate(xs):
        col = []
        for yv in ys:
            dv_target = float(xv) if kind == "plane" else float(dv_pin)
            bc_target = float(yv) if kind == "plane" else float(xv)
            shot = physics.run_case(dv_target, bc_target, native=std_native,
                                    metrics=std_metrics, tier=tier)
            if shot.ok and shot.hit and math.isfinite(float(shot.t_hit)):
                col.append(round(float(shot.t_hit), 6))
                backend = backend or str(shot.backend or "")
            else:
                col.append(None)                    # 未命中 ⇒ nan 的 JSON 说法（**不是 0**）
            done += 1
            if done % 8 == 0 or done == total:
                _report(report, 0.05 + 0.9 * (done / total), f"{done}/{total} 个格点")
        grid.append(col)
    lines = physics.marching_squares(xs, ys, grid, physics.iso_levels(
        [v for col in grid for v in col if v is not None], iso_step))
    elapsed = (time.perf_counter() - t0) * 1000.0
    flat = [v for col in grid for v in col if v is not None]
    out = {
        "kind": kind,
        "keys": [r["key"] for r in rows],
        "standard": std_key,
        # ⚠ `axis` 报的是**轴域**（自适应轴，与主仓 overlay 同口径）；网格端点可能比它窄一点
        #   （范围按步长向内对齐）。实际用的步长另用 `x_step/y_step` 如实报出。
        "axis": {"xlim": [float(axis["xlim"][0]), float(axis["xlim"][1])],
                 "ylim": [float(axis["ylim"][0]), float(axis["ylim"][1])],
                 "x_step": x_step, "y_step": y_step},
        "iso_lines": lines,
        "t_range_s": [min(flat), max(flat)] if flat else None,
        "hits": len(flat),
        "nodes": total,
        "notes": [
            f"等时线属于基准弹 {std_key}（池内短名）；口径 = 主仓 grid.make_grid / iso_levels / "
            "extract_isolines（线性等值线）",
            f"扫描网格 ΔV 步长 {x_step:g}"
            + (f" × BC 步长 {y_step:g}" if kind == "plane" else f"（β）× ginv 步长 {y_step:g}")
            + f"，共 {total} 个格点；iso_step = {iso_step:g} s",
            f"未命中的格点按 nan 处理（不出现在等时线里）；命中 {len(flat)} / {total}",
            f"tier = {tier}，求解器后端 = {backend or 'unreported'}（真跑过才写）",
        ],
        "identity": physics.identity_block(tier=tier, backend=backend, cached=False,
                                          elapsed_ms=elapsed, nodes=total),
    }
    if cache is not None:
        cache.put(key, out)
    return out


def _standard(std_key: str):
    pkg = physics._pkg()
    entry = pkg.pool.resolve(std_key)
    metrics = pkg.pool.metrics_of(entry)
    nominal = {"key": entry.key, "native": entry.native, "dv": float(metrics.dv),
               "bc": float(metrics.bc)}
    rec = physics.catalog_record()
    for m in rec.get("missiles") or []:
        if str(m.get("key")) == entry.key:
            nominal["ginv"] = float(m.get("ginv"))
            nominal["gamma"] = float(m.get("gamma"))
            break
    return entry.native, metrics, nominal


def _report(report, frac: float, note: str = "") -> None:
    if report is not None:
        report(float(frac), note)


# --------------------------------------------------------------------- 等时面
def isosurface(keys=None, *, tier: str | None = None, level_s: float = 1.0, grid: int | None = None,
               sync: bool = True, report=None, cache=None) -> dict:
    """等时面：**单枚弹**的 (ΔV, β, ginv) 立方体里 `t_hit == level_s` 的等值面（marching tetrahedra）。

    * 立方体窗口：以该弹标称点为中心，ΔV ±10% / β ±10% / ginv ±20%（`grid` 是每轴采样数）；
    * `ginv` 由 `cxaoa_scale = ginv_nominal / ginv_target` 施加（γ ∝ CxAoA ⇒ ginv ∝ 1/CxAoA，
      系数从目录里的 `gamma`/`ginv` 现取 —— 与主仓"注入前落回物理 γ"的口径一致）；
    * 物理仍是 `solver.run()` 一次一个顶点，**没有**在外壳重算任何公式。
    """
    ks = [str(k) for k in (keys or [])]
    if len(ks) != 1:
        raise config.bad_request("bad_keys", "等时面一次只算**一枚**弹（keys 给一个）")
    tier = (tier or config.tier_default()).lower()
    n = int(grid or DEFAULT_CUBE_N)
    if n < 2 or n > 24:
        raise config.bad_request("bad_grid", f"grid 要在 2..24 之间（收到 {n}）")
    nodes = n ** 3
    limit = config.sync_max_nodes() if sync else config.job_max_nodes()
    if nodes > limit:
        raise config.Problem(413, "grid_too_large",
                             f"等时面要 {n}³ = {nodes} 个顶点，超过上限 {limit} —— 请降 grid 或走 /v1/jobs")
    rows, _axis = _axis_rows("bg", ks)
    row = rows[0]
    dv0, bc0, ginv0 = float(row["dv"]), float(row["bc"]), float(row["ginv"])
    dv_axis = (dv0 * 0.9, dv0 * 1.1)
    bc_axis = (bc0 * 0.9, bc0 * 1.1)
    ginv_axis = (ginv0 * 0.8, ginv0 * 1.2)
    key = physics.cache_key({"op": "isosurface", "key": row["key"], "tier": tier,
                             "level_s": float(level_s), "n": n})
    if cache is not None:
        hit = cache.get(key)
        if hit is not None:
            out = dict(hit)
            out["identity"] = dict(out.get("identity") or {})
            out["identity"]["cached"] = True
            out["identity"]["elapsed_ms"] = 0.0
            return out
    t0 = time.perf_counter()
    xs = [dv_axis[0] + (dv_axis[1] - dv_axis[0]) * i / (n - 1) for i in range(n)]
    ys = [bc_axis[0] + (bc_axis[1] - bc_axis[0]) * i / (n - 1) for i in range(n)]
    zs = [ginv_axis[0] + (ginv_axis[1] - ginv_axis[0]) * i / (n - 1) for i in range(n)]
    native, metrics = row["native"], None
    pkg = physics._pkg()
    metrics = pkg.pool.metrics_of(pkg.pool.resolve(row["key"]))
    values = {}
    backend = ""
    done, total = 0, n ** 3
    for i, xv in enumerate(xs):
        for j, yv in enumerate(ys):
            for k, zv in enumerate(zs):
                shot = physics.run_case(xv, yv, native=native, metrics=metrics, tier=tier,
                                        cxaoa_scale=ginv0 / float(zv))
                backend = backend or str(shot.backend or "")
                values[(i, j, k)] = (round(float(shot.t_hit), 6)
                                     if (shot.ok and shot.hit and math.isfinite(float(shot.t_hit)))
                                     else None)
                done += 1
                if done % 8 == 0 or done == total:
                    _report(report, 0.05 + 0.9 * (done / total), f"{done}/{total} 个顶点")
    mesh = _marching_tetrahedra(xs, ys, zs, values, float(level_s))
    elapsed = (time.perf_counter() - t0) * 1000.0
    out = {
        "kind": "fig3",
        "level_s": float(level_s),
        "key": row["key"],
        "mesh": mesh,
        "axis": {"dv": [float(xs[0]), float(xs[-1])], "bc": [float(ys[0]), float(ys[-1])],
                 "ginv": [float(zs[0]), float(zs[-1])], "n": n},
        "nodes": total,
        "notes": [
            f"等值面 = t_hit == {float(level_s):g} s 的 marching tetrahedra（每轴 {n} 个采样）",
            "窗口：以该弹标称点为中心 ΔV ±10% / β ±10% / ginv ±20%",
            "ginv 通过 cxaoa_scale = ginv标称 / ginv目标 施加（γ ∝ CxAoA，系数取目录里的 gamma/ginv）",
            f"tier = {tier}，求解器后端 = {backend or 'unreported'}",
        ],
        "identity": physics.identity_block(tier=tier, backend=backend, cached=False,
                                           elapsed_ms=elapsed, nodes=total),
    }
    if cache is not None:
        cache.put(key, out)
    return out


#: 立方体的 6 个四面体（用 0..7 的顶点编号，先按 (i,j,k) 升序编号）。
_TETS = ((0, 5, 1, 6), (0, 1, 2, 6), (0, 2, 3, 6), (0, 3, 7, 6), (0, 7, 4, 6), (0, 4, 5, 6))


def _marching_tetrahedra(xs, ys, zs, values, level: float) -> dict:
    """立方体 → 6 个四面体 → 逐个提三角形；顶点去重后返回 `{vertices, indices}`。"""
    verts: list[list[float]] = []
    index: dict[tuple, int] = {}
    tris: list[list[int]] = []

    def vid(p):
        k = (round(p[0], 9), round(p[1], 9), round(p[2], 9))
        i = index.get(k)
        if i is None:
            i = len(verts)
            index[k] = i
            verts.append([k[0], k[1], k[2]])
        return i

    for i in range(len(xs) - 1):
        for j in range(len(ys) - 1):
            for k in range(len(zs) - 1):
                cube = [(i, j, k), (i + 1, j, k), (i + 1, j + 1, k), (i, j + 1, k),
                        (i, j, k + 1), (i + 1, j, k + 1), (i + 1, j + 1, k + 1), (i, j + 1, k + 1)]
                pts = [(xs[a], ys[b], zs[c]) for (a, b, c) in cube]
                vals = [values.get(c) for c in cube]
                if any(v is None for v in vals):
                    continue                    # 有未命中 ⇒ 这个格子不参与（口径：nan 不是 0）
                for tet in _TETS:
                    tri = _tet_triangle([pts[t] for t in tet], [float(vals[t]) for t in tet], level)
                    if tri:
                        tris.append([vid(p) for p in tri])
    return {"vertices": verts, "indices": [x for t in tris for x in t],
            "triangles": len(tris)}


def _tet_triangle(p, v, level: float):
    """一个四面体内的等值三角形（标准 marching tetrahedra，最多两个三角形取第一个）。"""
    inside = [i for i in range(4) if v[i] >= level]
    outside = [i for i in range(4) if v[i] < level]
    if len(inside) == 0 or len(inside) == 4:
        return None
    if len(inside) == 1 or len(inside) == 3:
        one = inside[0] if len(inside) == 1 else outside[0]
        others = [i for i in range(4) if i != one]
        out = []
        for o in others:
            if abs(v[o] - v[one]) < 1e-12:
                continue
            f = (level - v[one]) / (v[o] - v[one])
            out.append(tuple(p[one][d] + (p[o][d] - p[one][d]) * f for d in range(3)))
        return out[:3] if len(out) >= 3 else None
    # 2-2 分裂：两对交点各给一条边，取 4 点组成的四边形剖成两个三角形（这里只取前三条边）
    pairs = [(i, o) for i in inside for o in outside]
    out = []
    for i, o in pairs:
        if abs(v[o] - v[i]) < 1e-12:
            continue
        f = (level - v[i]) / (v[o] - v[i])
        out.append(tuple(p[i][d] + (p[o][d] - p[i][d]) * f for d in range(3)))
    return out[:3] if len(out) >= 3 else None


def big_overlay_spec(kind: str, keys, **kw) -> dict:
    """把 `overlay()` 的调用参数打成一个 dict，交给 `/v1/jobs`（保持参数解析只写一处）。"""
    return {"kind": kind, "keys": list(keys or []), **kw}


def grid_estimate(kind: str, keys, *, dv_step=None, bc_step=None, beta_step=None,
                  ginv_step=None) -> dict:
    """不跑解算的**成本预估**（给客户端决定走同步还是任务）：节点数 + 是否超同步上限。"""
    rows, axis = _axis_rows(kind, keys)
    try:
        x, y, nodes = grid_steps(kind, axis, dv_step=dv_step, bc_step=bc_step,
                                 beta_step=beta_step, ginv_step=ginv_step,
                                 max_nodes=config.job_max_nodes())
    except config.Problem as exc:
        return {"ok": False, "error": exc.payload()["error"], "axis": axis}
    return {"ok": True, "axis": axis, "dv_step": x, "bc_step": y, "nodes": nodes,
            "sync_ok": nodes <= config.sync_max_nodes()}
