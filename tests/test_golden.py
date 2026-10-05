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
