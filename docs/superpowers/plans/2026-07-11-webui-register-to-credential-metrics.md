# WebUI Register-to-Credential Metrics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 WebUI 运行台进度区实时显示「注册到凭证」平均速度（个/分钟）和成功率。

**Architecture:** `BatchRunner` 记录批次起止 monotonic 时间，在 `snapshot()` 计算 `elapsed_sec` / `avg_success_per_min` / `success_rate`；前端 `renderSnapshot` 只格式化这些字段并追加第二行进度文案。不新增 API，不改 TUI/历史列表 UI。

**Tech Stack:** Python 3 + unittest、现有 `http_batch_service.BatchRunner`、WebUI 静态 `app.js` / `app.css`

## Global Constraints

- 速度 = `succeeded / (elapsed_sec / 60)`，单位 `个/分钟`
- 成功率 = `succeeded / completed`（0~1）
- `elapsed_sec < 1` → 速度 `null`
- `completed == 0` → 成功率 `null`
- 仅渲染 WebUI 运行台进度区
- 前端不重算指标，只格式化后端字段
- TDD：先红后绿

---

## File Map

| 文件 | 职责 |
|---|---|
| `http_batch_service.py` | 计时状态 + snapshot 指标字段 |
| `tests/test_http_batch_service.py` | 指标计算单元测试 |
| `webui/static/app.js` | 进度区两行渲染 |
| `webui/static/app.css` | 两行 stats 微调 |
| `docs/superpowers/specs/2026-07-11-webui-register-to-credential-metrics-design.md` | 已确认设计（只读参考） |

---

### Task 1: 后端 snapshot 指标

**Files:**
- Modify: `http_batch_service.py`
- Test: `tests/test_http_batch_service.py`

**Interfaces:**
- Consumes: 现有 `BatchRunner.snapshot()` / `start()` / `_finalize()`
- Produces:
  - `BatchRunner.started_at_wall: Optional[str]`
  - `BatchRunner.started_at_monotonic: Optional[float]`
  - `BatchRunner.finished_at_monotonic: Optional[float]`
  - snapshot keys: `started_at`, `elapsed_sec`, `avg_success_per_min`, `success_rate`

- [ ] **Step 1: 写失败测试**

在 `tests/test_http_batch_service.py` 末尾追加：

```python
class SnapshotMetricsTests(unittest.TestCase):
    def _make_runner(self, count: int = 2) -> svc.BatchRunner:
        plan = svc.RunPlan(
            run_mode=svc.RUN_MODE_REGISTER_OTP,
            count=count,
            workers=1,
            output_dir=Path("."),
            proxy_mode="none",
            proxy_args=[],
            turnstile_provider="capsolver",
            turnstile_headless=False,
            email_provider="yyds",
            warnings=[],
            sso_convert_retries=5,
            sso_convert_cooldown=3,
        )
        return svc.BatchRunner(plan)

    def test_snapshot_metrics_before_start(self):
        runner = self._make_runner()
        snap = runner.snapshot()
        self.assertEqual(snap["elapsed_sec"], 0)
        self.assertIsNone(snap["avg_success_per_min"])
        self.assertIsNone(snap["success_rate"])
        self.assertEqual(snap["started_at"], "")

    def test_snapshot_metrics_running(self):
        runner = self._make_runner(count=3)
        runner.started = True
        runner.started_at_wall = "2026-07-11T12:00:00"
        runner.started_at_monotonic = 1000.0
        runner.workers[0].status = "succeeded"
        runner.workers[1].status = "failed"
        runner.workers[2].status = "running"
        with mock.patch.object(svc.time, "monotonic", return_value=1120.0):
            snap = runner.snapshot()
        self.assertEqual(snap["elapsed_sec"], 120)
        self.assertEqual(snap["completed"], 2)
        self.assertEqual(snap["succeeded"], 1)
        self.assertAlmostEqual(snap["avg_success_per_min"], 0.5)
        self.assertAlmostEqual(snap["success_rate"], 0.5)
        self.assertEqual(snap["started_at"], "2026-07-11T12:00:00")

    def test_snapshot_metrics_freeze_after_finalize_time(self):
        runner = self._make_runner(count=1)
        runner.started = True
        runner.started_at_wall = "2026-07-11T12:00:00"
        runner.started_at_monotonic = 1000.0
        runner.finished_at_monotonic = 1060.0
        runner.workers[0].status = "succeeded"
        with mock.patch.object(svc.time, "monotonic", return_value=9999.0):
            snap1 = runner.snapshot()
            snap2 = runner.snapshot()
        self.assertEqual(snap1["elapsed_sec"], 60)
        self.assertEqual(snap2["elapsed_sec"], 60)
        self.assertAlmostEqual(snap1["avg_success_per_min"], 1.0)
        self.assertAlmostEqual(snap1["success_rate"], 1.0)
```

- [ ] **Step 2: 跑测试确认失败**

Run:

```bash
python -m pytest tests/test_http_batch_service.py::SnapshotMetricsTests -v
```

Expected: FAIL（缺少字段 / AttributeError）

- [ ] **Step 3: 最小实现**

1. `BatchRunner.__init__` 增加：

```python
self.started_at_wall: Optional[str] = None
self.started_at_monotonic: Optional[float] = None
self.finished_at_monotonic: Optional[float] = None
```

2. `start()` 在首次启动时设置：

```python
self.started_at_monotonic = time.monotonic()
self.started_at_wall = time.strftime("%Y-%m-%dT%H:%M:%S")
```

注意：放在 `if self.started: return` 之后、真正开始逻辑里，并确保 `self.started = True` 前后只写一次。

3. `_finalize()` 在 `self.done = True` 前：

```python
if self.finished_at_monotonic is None:
    self.finished_at_monotonic = time.monotonic()
```

4. 在 `snapshot()` 增加计算与字段：

```python
if self.started_at_monotonic is None:
    elapsed_sec = 0
elif self.finished_at_monotonic is not None:
    elapsed_sec = max(0, int(self.finished_at_monotonic - self.started_at_monotonic))
else:
    elapsed_sec = max(0, int(time.monotonic() - self.started_at_monotonic))

completed = self.completed
succeeded = self.succeeded
avg_success_per_min = (
    None if elapsed_sec < 1 else float(succeeded) / (elapsed_sec / 60.0)
)
success_rate = None if completed == 0 else float(succeeded) / float(completed)
```

并在返回 dict 中加入：

```python
"started_at": self.started_at_wall or "",
"elapsed_sec": elapsed_sec,
"avg_success_per_min": avg_success_per_min,
"success_rate": success_rate,
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m pytest tests/test_http_batch_service.py::SnapshotMetricsTests -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_http_batch_service.py http_batch_service.py
git commit -m "feat: expose register-to-credential speed metrics in batch snapshot"
```

---

### Task 2: WebUI 进度区渲染

**Files:**
- Modify: `webui/static/app.js`
- Modify: `webui/static/app.css`
- Manual verify: 运行台页面

**Interfaces:**
- Consumes: snapshot 字段 `avg_success_per_min`, `success_rate`, `elapsed_sec`
- Produces: `#progressStats` 两行文案

- [ ] **Step 1: 在 app.js 增加格式化 helper**

放在 `renderSnapshot` 前：

```javascript
function formatElapsed(sec) {
  const n = Math.max(0, Number(sec) || 0);
  if (n < 60) return `${n}s`;
  const m = Math.floor(n / 60);
  const s = n % 60;
  return `${m}m${String(s).padStart(2, "0")}s`;
}

function formatSpeed(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "-";
  return `${Number(v).toFixed(1)} 个/分钟`;
}

function formatRate(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "-";
  return `${(Number(v) * 100).toFixed(1)}%`;
}
```

- [ ] **Step 2: 改 renderSnapshot 进度文案**

把：

```javascript
$("progressStats").textContent =
  `run=${snap.run_id || "-"} | 完成 ${done}/${total} | 成功 ${snap.succeeded || 0} | 失败 ${snap.failed || 0} | 活动 ${snap.active || 0}`;
```

改成：

```javascript
const line1 = `run=${snap.run_id || "-"} | 完成 ${done}/${total} | 成功 ${snap.succeeded || 0} | 失败 ${snap.failed || 0} | 活动 ${snap.active || 0}`;
const line2 = `速度 ${formatSpeed(snap.avg_success_per_min)} | 成功率 ${formatRate(snap.success_rate)} | 耗时 ${formatElapsed(snap.elapsed_sec)}`;
$("progressStats").textContent = `${line1}\n${line2}`;
```

- [ ] **Step 3: CSS 微调**

`.stats` 增加：

```css
.stats {
  font-variant-numeric: tabular-nums;
  white-space: pre-line;
  line-height: 1.5;
}
```

- [ ] **Step 4: 静态/回归检查**

```bash
python -m pytest tests/test_http_batch_service.py tests/test_webui_app.py -v
```

Expected: PASS

手工检查点（如果本机 webui 可开）：

1. 未启动：第二行 `速度 - | 成功率 - | 耗时 0s`
2. 运行中：速度/成功率随 snapshot 更新
3. 完成后：耗时冻结

- [ ] **Step 5: Commit**

```bash
git add webui/static/app.js webui/static/app.css
git commit -m "feat: render register-to-credential speed and success rate in WebUI"
```

---

### Task 3: 收尾验证

**Files:**
- 无新增代码，只验证

- [ ] **Step 1: 全量相关测试**

```bash
python -m pytest tests/test_http_batch_service.py tests/test_webui_app.py -v
```

Expected: 全绿

- [ ] **Step 2: 快速 grep 验收字段**

```bash
rg -n "avg_success_per_min|success_rate|elapsed_sec|formatSpeed|formatRate" http_batch_service.py webui/static/app.js tests/test_http_batch_service.py
```

Expected: 后端、前端、测试都有命中

- [ ] **Step 3: 最终说明**

输出：

- 改了哪些文件
- 指标口径
- 如何在运行台看到效果

---

## Spec Coverage Check

| Spec 要求 | Task |
|---|---|
| 速度 = 成功/分钟 | Task 1 |
| 成功率 = 成功/已完成 | Task 1 |
| 起止时间冻结 | Task 1 |
| WebUI 两行渲染 | Task 2 |
| 不改 TUI/历史 UI | 全局遵守 |
| 测试 | Task 1 + Task 3 |
