# -*- coding: utf-8 -*-
"""`missile-backend` 自测（W1 交付物 2 的五组判据）。

设计口径：
* **不 mock 求解器**：等时线/等时面的判据都真跑 `solver.run()`（本机有编译好的 C 快路径 ⇒ 秒级）。
  只有"数据门"那条故意喂坏数据（那正是它要拒的情形）。
* 请求都用**小窗口**（`keys=["PL-12"]` + 显式步长）把成本压到秒级；形状/数值/契约该断的都断。

运行：`pytest -q`（或 `python -m pytest tests -q`）。需要 `DATA_DIR` 指向那份 168 blk 数据。
"""

from __future__ import annotations

import os
import time

import pytest
from fastapi.testclient import TestClient

from app import config, physics
from app.main import BUCKET, app
from app.runtime import TokenBucket

TINY = {"kind": "plane", "keys": ["PL-12"], "tier": "fast",
        "iso_step": 5.0, "dv_step": 10.0, "bc_step": 25.0}


@pytest.fixture()
def client():
    # 限流在测试里放开（专门那条自己换桶），lifespan 会跑数据门
    BUCKET.reset()
    with TestClient(app) as c:
        yield c
    BUCKET.reset()


@pytest.fixture(autouse=True)
def _generous_bucket(monkeypatch):
    import app.main as m

    monkeypatch.setattr(m, "BUCKET", TokenBucket(per_min=6000, burst=6000))


# --------------------------------------------------------------------- ① health + 数据门
def test_health_reports_solver_and_data(client):
    h = client.get("/v1/health")
    assert h.status_code == 200
    body = h.json()
    assert body["ok"] is True and body["version"]
    assert body["solver"]["model"] == "python-game-6dof-v1"
    assert body["solver"]["version"] == "2.59.0.28"
    assert len(body["solver"]["sha256"]) == 64
    assert body["data"]["dir_ok"] is True, body
    assert body["data"]["identity"]["version"] == "2.59.0.28"
    assert body["tier_default"] in ("standard", "fast")
    assert body["limits"]["max_keys"] == 168


def test_data_gate_refuses_to_start_on_bad_data(tmp_path, monkeypatch):
    """**数据门**：DATA_DIR 没设 / 指向空目录 ⇒ `data_gate()` 抛错（进程就该退出）。"""
    monkeypatch.delenv("DATA_DIR", raising=False)
    with pytest.raises(RuntimeError) as e1:
        physics.data_gate()
    assert "DATA_DIR" in str(e1.value)

    empty = tmp_path / "empty-data"
    empty.mkdir()
    monkeypatch.setenv("DATA_DIR", str(empty))
    with pytest.raises(RuntimeError) as e2:
        physics.data_gate()
    assert "拒绝启动" in str(e2.value)

    # 反向对照：真的那份数据要能过
    monkeypatch.setenv("DATA_DIR", os.environ.get("MB_TEST_GOOD_DATA") or GOOD_DATA)
    state = physics.data_gate()
    assert state["ok"] is True and state["identity"]["version"] == "2.59.0.28"


GOOD_DATA = r"E:\导弹包线图-release\vendor\wt-missile\inputs\resources\2.59.0.28"


# --------------------------------------------------------------------- ② 契约实现（形状 + 数值）
def test_catalog_matches_site_schema(client):
    cat = client.get("/v1/catalog").json()
    for key in ("ok", "schema", "generated_utc", "solver", "reference", "count",
                "computable", "missiles", "unsupported"):
        assert key in cat, f"目录缺 {key}"
    assert cat["count"] == 168 and cat["computable"] == 159 and len(cat["unsupported"]) == 9
    row = [m for m in cat["missiles"] if m["key"] == "PL-12"]
    assert row and row[0]["in_pool11"] is True and row[0]["native"] == "cn_pl12"


def test_overlay_shape_and_axis_and_identity(client):
    r = client.post("/v1/overlay", json=TINY)
    assert r.status_code == 200, r.text
    o = r.json()
    assert o["kind"] == "plane" and o["keys"] == ["PL-12"] and o["standard"] == "PL-12"
    assert set(o["axis"]) >= {"xlim", "ylim", "x_step", "y_step"}
    assert o["axis"]["x_step"] == 10.0 and o["axis"]["y_step"] == 25.0
    assert o["nodes"] == o["hits"] > 0
    assert o["iso_lines"], "等时线一条都没有 —— 窗口/层级口径不对"
    for line in o["iso_lines"]:
        assert isinstance(line["level"], float) and line["points"]
        assert all(len(p) == 2 for p in line["points"])
    levels = [l["level"] for l in o["iso_lines"]]
    assert levels == sorted(levels), "层级必须递增"
    assert all(abs(lv / 5.0 - round(lv / 5.0)) < 1e-9 for lv in levels), "层级必须是 iso_step 的整数倍"
    # 窄窗口里区间必然被夹在 iso_step 上，且与 --axes 口径一致（轴首尾就是网格首尾）
    assert o["axis"]["xlim"][0] <= min(p[0] for l in o["iso_lines"] for p in l["points"]) and \
           max(p[0] for l in o["iso_lines"] for p in l["points"]) <= o["axis"]["xlim"][1]
    ident = o["identity"]
    assert ident["tier"] == "fast" and ident["cached"] is False
    assert ident["backend"] in ("compiled-c", "python-standard", "python-fast", "python-legacy", "unreported")
    assert ident["elapsed_ms"] > 0 and ident["nodes"] == o["nodes"]
    assert ident["solver_sha256"] and ident["resource_sha256"]


def test_overlay_is_cached_on_the_second_call(client):
    """同请求第二次 ⇒ `cached:true`（键与主仓 `cache_key()` 同口径：tier/步长/窗口/版本哈希都进键）。"""
    client.post("/v1/overlay", json=TINY)
    again = client.post("/v1/overlay", json=TINY).json()
    assert again["identity"]["cached"] is True
    assert again["iso_lines"], "缓存命中的那一份也要有内容"
    other = client.post("/v1/overlay", json={**TINY, "iso_step": 2.5}).json()
    assert other["identity"]["cached"] is False, "iso_step 变了就不该命中同一份缓存"


def test_overlay_physics_matches_solver_directly():
    """**数值口径**：等时线的 t_hit 就是 `solver.run()` 的值（这里直连一次标准弹标称点对拍）。

    45.185 s = PL-12 在默认场景（50 km 迎头 / 10 km）标称缩放的命中时间（本机实测，容差 0.05 s）。
    """
    pkg = physics._pkg()
    entry = pkg.pool.resolve("PL-12")
    shot = physics.run_case(932.57, 3078.05, native=entry.native,
                            metrics=pkg.pool.metrics_of(entry), tier="fast")
    assert shot.ok and shot.hit
    assert abs(float(shot.t_hit) - 45.185) < 0.05, shot.t_hit


def test_overlay_rejects_bad_requests(client):
    assert client.post("/v1/overlay", json={**TINY, "kind": "nope"}).status_code == 400
    assert client.post("/v1/overlay", json={**TINY, "tier": "pro"}).status_code == 400
    assert client.post("/v1/overlay", json={**TINY, "iso_step": 0.01}).status_code == 400
    too_many = client.post("/v1/overlay", json={**TINY, "keys": ["PL-12"] * 200})
    assert too_many.status_code == 400 and "keys" in too_many.text
    # 显式步长导致网格超上限 ⇒ 413（提示走 /v1/jobs）
    big = client.post("/v1/overlay", json={**TINY, "keys": None, "dv_step": 1.0, "bc_step": 1.0})
    assert big.status_code == 413 and "jobs" in big.text


# --------------------------------------------------------------------- ③ CORS 预检
def test_cors_preflight_allows_site_and_localhost_only(client):
    site = "https://strinova-xinghui.github.io"
    r = client.options("/v1/overlay", headers={"Origin": site,
                                               "Access-Control-Request-Method": "POST",
                                               "Access-Control-Request-Headers": "content-type"})
    assert r.status_code in (200, 204)
    assert r.headers.get("access-control-allow-origin") == site
    assert "POST" in r.headers.get("access-control-allow-methods", "")

    local = client.options("/v1/overlay", headers={"Origin": "http://127.0.0.1:8123",
                                                   "Access-Control-Request-Method": "POST"})
    assert local.headers.get("access-control-allow-origin") == "http://127.0.0.1:8123"

    evil = client.options("/v1/overlay", headers={"Origin": "https://evil.example",
                                                  "Access-Control-Request-Method": "POST"})
    assert evil.headers.get("access-control-allow-origin") is None, "非白名单来源不许放行"

    get_evil = client.get("/v1/catalog", headers={"Origin": "https://evil.example"})
    assert get_evil.headers.get("access-control-allow-origin") is None


def test_cors_origin_list_rejects_wildcard(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "*")
    with pytest.raises(ValueError):
        config.cors_origins()


# --------------------------------------------------------------------- ④ 限流
def test_rate_limit_returns_429_with_readable_body(client, monkeypatch):
    import app.main as m

    monkeypatch.setattr(m, "BUCKET", TokenBucket(per_min=1, burst=1))
    first = client.get("/v1/catalog")
    assert first.status_code == 200
    second = client.get("/v1/catalog")
    assert second.status_code == 429
    body = second.json()
    assert body["ok"] is False and body["error"]["code"] == "rate_limited"
    assert "请求太频繁" in body["error"]["message"]
    assert float(second.headers["Retry-After"]) >= 1
    # health 不参与限流（探针要能一直问）
    assert client.get("/v1/health").status_code == 200


# --------------------------------------------------------------------- ⑤ 后台任务
def test_jobs_state_machine_runs_to_done_with_monotone_progress(client):
    r = client.post("/v1/jobs", json={"op": "isosurface", "keys": ["PL-12"], "tier": "fast",
                                      "level_s": 45.0, "grid": 3})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    seen, states = [], []
    for _ in range(240):
        got = client.get(f"/v1/jobs/{job_id}").json()
        states.append(got["state"])
        seen.append(got["progress"])
        if got["state"] in ("done", "error"):
            break
        time.sleep(0.25)
    assert states[-1] == "done", got
    assert seen == sorted(seen), f"进度必须单调不减：{seen}"
    assert seen[-1] == 1.0
    res = got["result"]
    assert res["kind"] == "fig3" and res["level_s"] == 45.0
    assert res["axis"]["n"] == 3 and res["nodes"] == 27
    assert len(res["mesh"]["indices"]) % 3 == 0
    assert "running" in states, f"总得跑过 running：{states}"


def test_jobs_404_for_unknown_id(client):
    got = client.get("/v1/jobs/deadbeef")
    assert got.status_code == 404 and got.json()["error"]["code"] == "job_not_found"
