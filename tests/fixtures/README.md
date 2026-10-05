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

4. **bg 的 ginv 没进物理（第四处，也是最终那处）**：bg 两条用例的等值线一开始**整条压在同一 β 列**
   （`β=3129.252981` 的竖线），主仓是 β `3125.72 → 3138.05` 的斜线。**先量场再下结论**才分辨出：
   竖线的表象既可能是"场转置"，也可能是"某一维压根没进物理"—— 实测是后者：适配层只把 `(dv_pin, β)`
   喂给 `mapping.scaling_for()`，**ginv 只是画出来的坐标** ✗。
   **口径按主仓复刻（单一出处，不自己推公式）**：主仓 `workflow.overlay_bg()` 用的是
   `missile_solver.bg.scaling_for(point, metrics, beta=β_t, ginv=ginv_t, dv=ΔV_pin)`
   （β 走 `mapping.scaling_for` 的 `bc_target` 位置；ginv 由包里的 `gamma_of()` 落回物理 γ 再算
   `cxaoa_scale`）⇒ 本仓 `physics.run_case_bg()` 直接调它 ✓ 两条 bg 用例的最大偏差 **9.9e-5**（容差 1e-3）。

**结论**：四条用例现在全是**真判据**（无 `xfail`）。另加一条更快的"转置/轴互换"判据
（`test_marching_squares_catches_a_transposed_field`：`t = x + 4y` 的跨度比必须是 4，场转置后必变 0.25 ⇒ 红）。
