# -*- coding: utf-8 -*-
"""共享夹具：`client`（跑 lifespan ⇒ 数据门）与"测试期间把限流放开"（专门那条自己换桶）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import BUCKET, app
from app.runtime import TokenBucket


@pytest.fixture(autouse=True)
def _generous_bucket(monkeypatch):
    import app.main as m

    monkeypatch.setattr(m, "BUCKET", TokenBucket(per_min=6000, burst=6000))


@pytest.fixture()
def client():
    BUCKET.reset()
    with TestClient(app) as c:
        yield c
    BUCKET.reset()
