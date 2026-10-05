# 金标夹具：`overlay_golden.json`

**来源**：主仓（本机 `E:\导弹包线图-release`）**提交 `7a89819`**（"阶段 C · W2：workflow.overlay_plane/overlay_bg
只产 JSON（+ CLI --overlay --json）"）的 `--overlay` 纯 JSON 输出。

**生成命令**（逐条，`$env:PYTHONUTF8=1; $env:PYTHONPATH=src`，工作目录 = 主仓根）：

```powershell
python -m missile_sim --overlay plane --axes PL-12,MICA        --iso-step 5 --dv-step 20   --bc-step 50   --json
python -m missile_sim --overlay plane --axes PL-12,MICA,Derby  --iso-step 5 --dv-step 20   --bc-step 50   --json
python -m missile_sim --overlay bg    --axes PL-12,MICA        --iso-step 5 --beta-step 60 --ginv-step 8e-4 --json
python -m missile_sim --overlay bg    --axes PL-12,Derby       --iso-step 5 --beta-step 60 --ginv-step 8e-4 --json
```

* **为什么步长比默认粗**：默认是 plane 10/25、bg 30/4e-4；这里故意放宽（20/50、60/8e-4），
  好让 `pytest` 里那条判据真跑得动（每条 26~50 个格点）。**口径没变**：网格仍按步长向内对齐、
  层级仍取整 `iso_step`、等值线仍是线性等值线。
* `tier=standard`（不用 `fast`）：免得依赖本机编译器/缓存。

**判据怎么比**：`tests/test_golden.py::test_overlay_matches_golden_fixture` ——
`axis.xlim/ylim` 舍入到 6 位后必须**相等**；`iso_lines` 的**层级序列必须逐值相等**；
每层点集合排序后**逐个点按 `tol`（默认 1e-3）比**。夹具里 `"xfail"` 字段存在时，
比较失败会报 `xfail`（不算通过也不算红），并在信息里贴出实测差异。

---

## 为什么要有这份夹具（它已经抓到三处漂移）

1. **bg 的步长口径**（最严重）：适配层最初把 plane 的 `dv_step/bc_step`（10/25）复用到 bg 上。
   而 bg 的纵轴是 `ginv ≈ 0.015`（1e-4 量级）⇒ 一步跨完整根轴、网格只剩一列，等时线全错。
   主仓的口径是 `BgRequest.beta_step = 30.0` / `ginv_step = 4e-4`（`workflow.py:221-222`）⇒ 已按它对齐
   （`app/overlay.py` 里 `DEFAULT_BETA_STEP/DEFAULT_GINV_STEP`，两种 kind 的步长分开，别复用）。
2. **轴的落位小数**：主仓 `figures.adaptive_axis()` 把轴**落定到 3 位小数**
   （`AxisSpec(xlim=(round(x0, 3), round(x1, 3)))`），适配层原先算到 6 位 ⇒ `920.569749` vs `920.57`。
   已对齐（plane 轴落 3 位；bg 轴主仓不落位，保持原样）。
3. **等值线的折线形态**：适配层最初把"每个格子的两个交点"直接拼起来，相邻格子共享的交点被记了两次
   （实测 8 点 vs 主仓 5 点）⇒ 改成先收"格子 → 一段"，再按端点**串成折线**（与 `contour.allsegs` 同形态）。

**仍然挂着的一处（已标 `xfail`，等主仓裁决）**：bg 两条用例里，本仓的等值线 6 个点全落在同一 β 列
（`3129.252981`），主仓是 β `3125.72 → 3138.05` 的斜线 —— 疑点：两边 bg 的网格布局可能是**转置**关系
（plane 的 ΔV–β 两条已逐点相同，所以不是插值/层级的问题）。裁决后删掉 `xfail` 字段即可。
