# -*- coding: utf-8 -*-
"""W1 追加判据：`presets` 与站点同值 + 等时线口径金标夹具（夹具到位即生效）。

* `presets`：本仓 `/v1/catalog` 的 `presets` 必须与站点 `webui._catalog_presets()` **逐值相同**
  —— 两处规则钉成一处事实（两边都从求解器包的 `pool`/`in_pool11` 现算）。
* 金标夹具：主仓 `--overlay … --json` 产出的 `tests/fixtures/overlay_golden.json` 一进仓，
  下面那条就从 skip 变成真判据（逐级同值 + 点集合按容差比）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app import physics

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "overlay_golden.json"


def test_catalog_presets_match_site(client):
    got = client.get("/v1/catalog").json()["presets"]
    assert got and got[0]["label"] == "仅主动弹"
    keys = sorted(got[0]["keys"])
    assert len(keys) == 11, keys
    # 反向对照：这 11 枚就是求解器包 pool 里的 in_pool11 那批（不复制规则）
    pkg = physics._pkg()
    assert keys == sorted(str(p.key) for p in pkg.pool.points(None))

    site_src = Path("E:/导弹包线图-release/src")
    if not site_src.is_dir():
        pytest.skip("主仓源码不在本机 ⇒ 只跑自洽那条（上面已断）")
    import sys
    sys.path.insert(0, str(site_src))
    try:
        from missile_sim import webui
        site = webui._catalog_presets(webui.catalog.load())
    except Exception as exc:                                        # noqa: BLE001
        pytest.skip(f"主仓 import 失败（{type(exc).__name__}: {exc}）—— 只跑自洽那条")
    assert got == site, f"后端 presets 与站点不一致：\n后端 {got}\n站点 {site}"


def test_overlay_matches_golden_fixture(client):
    """金标夹具（主仓 `--overlay … --json` 产出）在仓里 ⇒ 逐级 + 逐点比；不在 ⇒ skip（记在 README 的债里）。"""
    if not FIXTURE.is_file():
        pytest.skip("还没有 tests/fixtures/overlay_golden.json（等 W2 的 --overlay CLI 落地后生成）")
    golden = json.loads(FIXTURE.read_text(encoding="utf-8"))
    tier = os.environ.get("GOLDEN_TIER", "fast")
    assert golden.get("requests"), "夹具里得有 requests（每条 = 一次 overlay 请求 + 期望响应）"
    for case in golden["requests"]:
        req = dict(case["request"])
        req.setdefault("tier", tier)
        got = client.post("/v1/overlay", json=req).json()
        exp = case["response"]
        try:
            _compare_case(case, got, exp)
        except AssertionError as exc:
            if case.get("xfail"):
                pytest.xfail(f"{case.get('name')}: {case['xfail']} ｜ 实测差异：{exc}")
            raise
    return


def _compare_case(case, got, exp):
    if True:
        assert [round(float(v), 6) for v in got["axis"]["xlim"]] == \
               [round(float(v), 6) for v in exp["axis"]["xlim"]], case.get("name")
        assert [round(float(v), 6) for v in got["axis"]["ylim"]] == \
               [round(float(v), 6) for v in exp["axis"]["ylim"]], case.get("name")
        g_lv = [round(float(l["level"]), 6) for l in got["iso_lines"]]
        e_lv = [round(float(l["level"]), 6) for l in exp["iso_lines"]]
        assert g_lv == e_lv, f"{case.get('name')}: 层级不一致 {g_lv} vs {e_lv}"
        tol = float(case.get("tol", 1e-3))
        for gl, el in zip(got["iso_lines"], exp["iso_lines"]):
            gpts = sorted((round(float(x), 6), round(float(y), 6)) for x, y in gl["points"])
            epts = sorted((round(float(x), 6), round(float(y), 6)) for x, y in el["points"])
            assert len(gpts) == len(epts), f"{case.get('name')}: 第 {gl['level']} 层点数不同"
            for (gx, gy), (ex, ey) in zip(gpts, epts):
                assert abs(gx - ex) <= tol and abs(gy - ey) <= tol, \
                    f"{case.get('name')}: 第 {gl['level']} 层点不同 ({gx},{gy}) vs ({ex},{ey})"


# --------------------------------------------------------------------- 转置/轴互换的快速判据
def _axis_ratio(xs, ys, f, level: float) -> float:
    """给定"值 = f(x, y)"的解析场，抽等值线后返回**横轴跨度 / 纵轴跨度**。"""
    from app import physics as _ph

    field = [[f(x, y) for y in ys] for x in xs]          # 本模块约定：field[i_x][j_y]
    lines = _ph.marching_squares(xs, ys, field, [level])
    assert lines, "解析场里没抽出等值线 —— 构造有问题"
    pts = lines[0]["points"]
    dx = max(p[0] for p in pts) - min(p[0] for p in pts)
    dy = max(p[1] for p in pts) - min(p[1] for p in pts)
    return float("inf") if dy <= 0 else dx / dy


def test_marching_squares_catches_a_transposed_field():
    """**转置/轴互换的指纹**：`t = x + 4y` 的等值线满足 Δx/Δy = −4 ⇒ 跨度比 ≈ 4；
    把场按"第一维是 y"错填（即行列互换）后，跨度比会变成 ≈ 0.25 ⇒ 必须与 4 差得远（红）。

    这条比夹具快得多，专门盯"场数组的行列与 x/y 轴颠倒"这一类（主仓 contour 要 `(len(Y), len(X))`）。
    """
    xs = [0.0, 1.0, 2.0, 3.0, 4.0]
    ys = [0.0, 1.0, 2.0, 3.0, 4.0]
    right = _axis_ratio(xs, ys, lambda x, y: x + 4.0 * y, 9.0)
    assert abs(right - 4.0) < 0.05, f"正确朝向的跨度比应为 4，实测 {right}"

    swapped = _axis_ratio(xs, ys, lambda x, y: y + 4.0 * x, 9.0)   # 等价于把场转置着填
    assert abs(swapped - 4.0) > 1.0, f"转置后的跨度比竟然也像对的（{swapped}）—— 这条判据没牙"
