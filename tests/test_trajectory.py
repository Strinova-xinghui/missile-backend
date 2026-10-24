# -*- coding: utf-8 -*-
"""`/v1/trajectory` 弹道接口的判据（task-3 验收 + 三维那条反证）。

task-3 点名的四组：① 缓存命中 ② 换 scene 真进物理（反证）③ 点数与首尾 ④ 未知 key 是 4xx。
另外补了三条：**z 列必须真的存在**（默认迎头场景 z 恒为 0，最容易顺手把这一列丢掉）、
点列列名契约、以及**目录预设名也能直接算**（站点左栏列的是 159 条，不只 11 枚池）。

判据都真跑求解器（fast 档单枚约 0.15~0.2 s），不 mock。
"""

from __future__ import annotations

import pytest

TINY = {"keys": ["PL-12"], "tier": "fast"}
#: 带侧向的想定：x/z 都非零（默认迎头场景 z 恒为 0，验不出三维）。
BEARING_45 = {"target_bearing_deg": 45}


def _case(body) -> dict:
    return body["cases"][0]


# --------------------------------------------------------------------- ① 缓存
def test_trajectory_is_cached_on_the_second_call(client):
    """同 keys/tier/scene 第二次 ⇒ cached:true，且弹道与第一次逐点相同。"""
    from app.main import CACHE

    CACHE.clear()
    first = client.post("/v1/trajectory", json=TINY).json()
    assert first["identity"]["cached"] is False
    again = client.post("/v1/trajectory", json=TINY).json()
    assert again["identity"]["cached"] is True
    assert _case(again)["points"] == _case(first)["points"], "缓存命中的那份必须逐点一致"


# --------------------------------------------------------------------- ② scene 反证
def test_trajectory_scene_really_changes_physics_and_cache(client):
    """反证：改 scene（50 km vs 60 km）⇒ t_hit 必须不同，且不能命中旧缓存。

    这条守的是"scene 只进了缓存键、没进物理" —— 那是最像正常的坏：缓存乖乖换新键、
    返回 200，弹道却一模一样。
    """
    near = client.post("/v1/trajectory", json={**TINY, "scene": {"range_m": 50000}}).json()
    far = client.post("/v1/trajectory", json={**TINY, "scene": {"range_m": 60000}}).json()
    assert far["identity"]["cached"] is False, "scene 变了却命中旧缓存 ⇒ 键里没带 scene"
    a, b = _case(near), _case(far)
    assert a["t_hit"] and b["t_hit"]
    assert abs(a["t_hit"] - b["t_hit"]) > 1.0, (
        f"50 km 与 60 km 的 t_hit 几乎相同（{a['t_hit']} vs {b['t_hit']}）⇒ scene 没进物理")
    assert b["t_hit"] > a["t_hit"], "飞得更远该飞得更久"
    assert a["points"] != b["points"]


# --------------------------------------------------------------------- ③ 抽稀
def test_trajectory_points_are_thinned_to_300_with_endpoints_kept(client):
    """点数 ≤ 300、首尾必留、时间严格递增；且响应里如实报原始行数与抽稀后点数。"""
    case = _case(client.post("/v1/trajectory", json=TINY).json())
    pts = case["points"]
    assert 2 <= len(pts) <= 300, f"点数 {len(pts)} 不在 2..300"
    assert case["n_raw"] > len(pts), "原始采样应该远多于 300（没抽稀？）"
    assert case["n_points"] == len(pts)
    assert pts[0][0] == 0.0, "首点必须是 t=0"
    ts = [p[0] for p in pts]
    assert ts == sorted(ts) and len(set(ts)) == len(ts), "时间必须严格递增"
    for p in pts:
        assert len(p) == 5, f"点必须是 [t, x, y, z, v]：{p}"
        assert p[1] is not None and p[2] is not None and p[4] is not None


def test_trajectory_thinning_keeps_shape_and_apex():
    """抽稀的**精度**判据（不走 HTTP）：300 点的首尾点与最高点都跟全量一致。

    lead 2026-10-24 实测：PL-12 全 2170 点 vs 抽到 300 点，apex 完全相同（12725.1 m）；
    时间步长均匀（dt 0.01945~0.02083 s 的浮点抖动）⇒ 等间距抽稀不会漏掉关键段。
    """
    from app import physics

    case = physics.run_trajectory("PL-12", tier="fast")
    thin = case["points"]
    assert len(thin) == 300 and case["n_raw"] > 300

    pkg = physics._pkg()
    raw = pkg.pool.resolve("PL-12")
    metrics = pkg.pool.metrics_of(raw)
    scaling = pkg.mapping.scaling_for(float(metrics.dv), float(metrics.bc), metrics)
    shot = pkg.solver.run(pkg.Scene(), missile=raw.native, scaling=scaling,
                          tier="fast", want_rows=True, want_cpa=False)
    full = shot.rows
    # 最高点：抽稀后与全量一致（容差 1 m，只吸收落位小数）
    apex_full = max(float(r["y"]) for r in full)
    apex_thin = max(p[2] for p in thin)
    assert abs(apex_full - apex_thin) <= 1.0, (apex_full, apex_thin)
    # 首尾真的钉在原轨迹的两端（不是"近似到头"）
    assert thin[0][1] == round(float(full[0]["x"]), 1)
    assert thin[0][2] == round(float(full[0]["y"]), 1)
    assert thin[-1][1] == round(float(full[-1]["x"]), 1)
    assert thin[-1][3] == round(float(full[-1]["z"]), 1)
    assert thin[-1][4] == round(float(full[-1]["v"]), 1)


# --------------------------------------------------------------------- ④ 错误
def test_trajectory_unknown_key_is_4xx_not_500(client):
    """未知弹名 ⇒ 400（带池内短名清单）；内核拒绝的型号 ⇒ 400；非法 tier / 超限 keys 也 4xx。"""
    r = client.post("/v1/trajectory", json={"keys": ["NOPE"], "tier": "fast"})
    assert r.status_code == 400, r.text
    body = r.json()
    assert body["ok"] is False and body["error"]["code"] == "unknown_key"
    assert "PL-12" in body["error"]["message"], "报错里该给池内短名清单"
    # 内核明确拒绝的型号（多发动机族 / 未移植的外环控制器）⇒ unsupported_key，同样 4xx
    bad = client.post("/v1/trajectory", json={"keys": ["cn_pl15"], "tier": "fast"})
    assert bad.status_code == 400, bad.text
    assert bad.json()["error"]["code"] == "unsupported_key"
    # 非法 tier 由 pydantic 拦成 400（与 overlay 同口径）
    assert client.post("/v1/trajectory", json={**TINY, "tier": "pro"}).status_code == 400
    # keys 超上限 ⇒ 400
    too_many = client.post("/v1/trajectory", json={"keys": ["PL-12"] * 200, "tier": "fast"})
    assert too_many.status_code == 400 and "keys" in too_many.text


# --------------------------------------------------------------------- ⑤ 三维（lead 追加）
def test_trajectory_is_really_three_dimensional_with_bearing(client):
    """三维反证：带方位角时 z 必须非零 —— 否则接口只表达了"距离-时间"。

    默认迎头场景 z 恒为 0，所以这条必须显式换 scene 才验得出来；
    同时确认默认场景下 z 也**仍然出现在点里**（是 0.0，不是被删掉）。
    """
    default = _case(client.post("/v1/trajectory", json=TINY).json())
    assert all(p[3] is not None for p in default["points"]), "默认场景也必须带 z 列（值是 0）"
    assert max(abs(p[3]) for p in default["points"]) < 1.0, "迎头场景 z 该是 0"

    turned = _case(client.post("/v1/trajectory",
                               json={**TINY, "scene": BEARING_45}).json())
    zs = [p[3] for p in turned["points"]]
    assert max(abs(z) for z in zs) > 1000.0, (
        f"target_bearing_deg=45 下 z 该到公里级，实际 max|z| = {max(abs(z) for z in zs)}")
    # 三维形状：x 与 z 都真的在变（不是某一条轴恒定）
    assert len({round(p[1], 1) for p in turned["points"]}) > 10
    assert len({round(p[3], 1) for p in turned["points"]}) > 10


# --------------------------------------------------------------------- ⑥ 契约
def test_trajectory_columns_contract(client):
    """点列是 [t, x, y, z, v]；columns / columns_units 与之一一对应；y 是**绝对高度**。"""
    o = client.post("/v1/trajectory", json=TINY).json()
    assert o["columns"] == ["t", "x", "y", "z", "v"]
    assert o["columns_units"] == ["s", "m", "m", "m", "m/s"]
    assert o["kind"] == "trajectory" and o["keys"] == ["PL-12"]
    case = _case(o)
    assert case["columns"] == o["columns"] and case["columns_units"] == o["columns_units"]
    # y 是绝对高度（发射高度 10 km 起），不是相对偏移：首点 y ≈ alt_m
    assert 9000 < case["points"][0][2] < 11000
    assert case["max_alt_m"] and case["max_alt_m"] > case["points"][0][2], "爬升过就该高于发射高度"
    assert o["identity"]["nodes"] == case["n_points"]
    assert o["identity"]["backend"] in ("compiled-c", "python-standard", "python-fast",
                                        "python-legacy", "unreported")


def test_trajectory_accepts_catalog_preset_names(client):
    """站点左栏列的是目录里的**全部可算条目**（159 条），不只 11 枚池 ⇒ 预设名也必须能直接算。

    2026-10-24 实测：只用 pool.resolve() 时，用户从列表点第 12 枚（atam_mistral）直接 400。
    """
    r = client.post("/v1/trajectory", json={"keys": ["atam_mistral"], "tier": "fast"})
    assert r.status_code == 200, r.text
    case = _case(r.json())
    assert case["key"] == "atam_mistral" and case["native"] == "atam_mistral"
    assert len(case["points"]) >= 2 and case["error"] == ""
    # 池内短名照旧（大小写不敏感，走 pool.resolve 那条）
    lower = client.post("/v1/trajectory", json={"keys": ["pl-12"], "tier": "fast"})
    assert lower.status_code == 200 and _case(lower.json())["key"] == "PL-12"


def test_trajectory_returns_all_pool_when_keys_empty(client):
    """keys 不给 ⇒ 池内 11 枚（**不是空结果**）。响应里 keys 与 cases 一一对应。"""
    o = client.post("/v1/trajectory", json={"tier": "fast"}).json()
    assert len(o["cases"]) == 11 and len(o["keys"]) == 11
    assert o["keys"] == [c["key"] for c in o["cases"]]
    assert all(c["points"] for c in o["cases"]), "每条都该有点"
    assert o["identity"]["nodes"] == sum(c["n_points"] for c in o["cases"])


def test_thin_points_is_pure_and_keeps_endpoints():
    """抽稀的纯函数判据（造数据，不跑求解器）：≤limit、首尾钉死、超长才抽。"""
    from app import physics

    rows = [{"t": float(i), "x": float(i), "y": float(i) * 2, "z": 0.0, "v": 1.0}
            for i in range(1000)]
    pts = physics.thin_points(rows, limit=300)
    assert len(pts) == 300
    assert pts[0][0] == 0.0 and pts[-1][0] == 999.0, "首尾必须钉死"
    # 短于上限 ⇒ 原样全给（含首尾），不补点也不删点
    short = physics.thin_points(rows[:50], limit=300)
    assert len(short) == 50 and short[0][0] == 0.0 and short[-1][0] == 49.0
    # 缺失值不许变成 0（那会被读成"测出来是 0"）
    holes = physics.thin_points([{"t": 0.0, "x": None, "y": float("nan")},
                                 {"t": 1.0, "x": 1.0, "y": 2.0}])
    assert holes[0][1] is None and holes[0][2] is None
    assert holes[1][1] == 1.0
