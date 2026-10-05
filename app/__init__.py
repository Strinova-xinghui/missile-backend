# -*- coding: utf-8 -*-
"""`missile-backend` —— 等时线/等时面的计算服务（W1）。

对外契约（v1，冻结）见 `README.md` 与 `main.py` 的路由表；本包只做计算，不做页面。
"""

from .config import APP_VERSION  # noqa: F401

__version__ = APP_VERSION
