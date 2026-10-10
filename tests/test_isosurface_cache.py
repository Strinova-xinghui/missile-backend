# -*- coding: utf-8 -*-
"""等时面的**立方体缓存**判据（W3b 时间滑块的前提）。

口径（Lead 2026-10-05 裁决）：立方体的 t 值按 `(key, tier, grid)` 缓存；`level_s` **不进**立方体键
⇒ 同一个立方体上改层级只重提面（毫秒级）。响应形状不变；`identity.cached` 表示"立方体来自缓存"。
"""

from __future__ import annotations

import pathlib

from app import overlay

APP = pathlib.Path(__file__).resolve().parents[1] / "app"


def _code() -> str:
    return (APP / "overlay.py").read_text(encoding="utf-8")


def _cube_cache_problems(code: str) -> list:
    """纯函数：立方体键不许带 `level_s`；提面键必须带；且立方体键要在提面之前算出来。"""
    bad = []
    i = code.index('"op": "isosurface_cube"')
    cube_key_line = code[code.rindex("physics.cache_key(", 0, i):code.index(")", i) + 1]
    if "level_s" in cube_key_line:
        bad.append("立方体缓存键里带了 level_s（每动一格就重扫）")
    j = code.index('mesh_key = ')
    mesh_line = code[j:code.index("\n", j)]
    if "level_s" not in mesh_line:
        bad.append("提面键（mesh_key）里没带 level_s ⇒ 不同层级会串味")
    if not (i < j):
        bad.append("立方体键没有在提面之前算出来")
    if "cache.put(cube_key" not in code or "cache.get(cube_key" not in code:
        bad.append("立方体没有真正读写缓存")
    return bad


def test_cube_cache_checker():
    code = _code()
    problems = _cube_cache_problems(code)
    assert problems == [], problems


def test_cube_cache_checker_has_teeth():
    """反证：把 `level_s` 塞回立方体键 ⇒ 必须红（这正是"滑块每动一格重扫 40 s"那个病）。"""
    code = _code()
    assert _cube_cache_problems(code) == []
    mutant = code.replace('{"op": "isosurface_cube", "key": row["key"], "tier": tier,\n'
                          '                                  "n": n, "dv0": dv0, "bc0": bc0, "ginv0": ginv0}',
                          '{"op": "isosurface_cube", "key": row["key"], "tier": tier,\n'
                          '                                  "level_s": float(level_s),\n'
                          '                                  "n": n, "dv0": dv0, "bc0": bc0, "ginv0": ginv0}', 1)
    assert mutant != code, "反证没改到源码"
    assert _cube_cache_problems(mutant), "把 level_s 塞回立方体键却抓不到"


def test_isosurface_cube_is_cached_across_levels(client):
    """**行为判据**：同 `(key, tier, grid)`、只改 `level_s` ⇒ 第二次 `cached=true` 且 elapsed 骤降。"""
    # ⚠ 与用例顺序无关：先清空进程内缓存（否则别的用例建过的立方体会让"第一次"也命中）
    from app.main import CACHE

    CACHE.clear()
    body = {"keys": ["PL-12"], "tier": "fast", "grid": 3}
    first = client.post("/v1/isosurface", json={**body, "level_s": 45.0}).json()
    assert first["identity"]["cached"] is False, "第一次不该命中立方体缓存"
    assert first["identity"]["cube_cached"] is False
    assert first["identity"]["elapsed_ms"] > 200, first["identity"]     # 真扫了 27 个顶点

    second = client.post("/v1/isosurface", json={**body, "level_s": 42.0}).json()
    assert second["identity"]["cached"] is True, "只改 level_s 却没命中立方体缓存 ⇒ 又重扫了"
    assert second["identity"]["cube_cached"] is True
    assert second["identity"]["elapsed_ms"] < first["identity"]["elapsed_ms"] / 10, \
        (first["identity"], second["identity"])
    assert second["level_s"] == 42.0 and second["axis"] == first["axis"], "同一立方体 ⇒ 轴必须一致"
    assert second["mesh"]["triangles"] != first["mesh"]["triangles"] or \
        len(second["mesh"]["vertices"]) != len(first["mesh"]["vertices"]), "换了层级，面该不一样"
