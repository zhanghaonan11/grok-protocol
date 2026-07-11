# Local Turnstile Max Workers Config Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 WebUI 配置中心暴露 `local_turnstile_max_workers`，让 local Turnstile 并发上限可配置，不再写死为 3。

**Architecture:** 在 `http_batch_service.py` 增加解析/校验函数；`build_plan()` 仅在 `turnstile_provider=local` 时用配置 cap 压缩 workers；配置中心 GET/PUT 读写该字段；WebUI 配置页增加数字输入。默认 3，合法范围 1~6666；总并发仍受 `MAX_WORKERS=32` 约束。

**Tech Stack:** Python 3 标准库、unittest、FastAPI/WebUI 静态页（现有 `webui/`）

## Global Constraints

- 字段名固定：`local_turnstile_max_workers`
- 默认：`3`（兼容现有 `MAX_LOCAL_TURNSTILE_WORKERS`）
- 范围：`1 ~ 6666`
- 仅 `turnstile_provider == "local"` 时参与并发压缩
- 总并发硬顶仍是 `MAX_WORKERS = 32`
- 配置中心保存：非法值严格失败（`TuiConfigError`）
- `build_plan` 运行时：非法/缺省防御性回落默认 3（必要时夹紧）
- local cap 警告与 YYDS 警告文案必须拆开
- 所有用户可见文案用中文；代码标识用英文
- 不改 YYDS 建邮间隔逻辑
- TDD：先写失败测试，再改实现

## File Map

| 文件 | 职责 |
|---|---|
| `http_batch_service.py` | 常量、解析函数、`build_plan` cap、config-center 读写 |
| `tests/test_http_batch_service.py` | cap 与配置中心单测 |
| `webui/templates/config.html` | 配置中心输入控件 |
| `webui/static/config.js` | 表单 fill/collect |
| `config.example.json` | 示例字段 |
| `USAGE.md` | 配置表补一行（若有配置字段表） |

---

### Task 1: Service 解析 + build_plan 使用可配置 cap

**Files:**
- Modify: `http_batch_service.py`（常量区约 L33-34；helpers 约 L403+；`build_plan` local 分支约 L606-622）
- Test: `tests/test_http_batch_service.py`

**Interfaces:**
- Consumes: 现有 `Settings.config`、`build_plan(settings)`、`TuiConfigError`、`_positive_int`
- Produces:
  - `MIN_LOCAL_TURNSTILE_WORKERS = 1`
  - `ABS_MAX_LOCAL_TURNSTILE_WORKERS = 6666`
  - `MAX_LOCAL_TURNSTILE_WORKERS = 3`（保留为默认）
  - `resolve_local_turnstile_max_workers(config, *, strict: bool = False) -> int`
  - `build_plan` 在 local 时用解析后的 cap

- [ ] **Step 1: 写失败测试（默认 cap + 配置 cap + 非 local）**

在 `tests/test_http_batch_service.py` 的 `HttpBatchServiceSmokeTests` 中，保留现有 `test_build_plan_local_caps_workers`，并新增：

```python
    def test_build_plan_local_uses_configured_cap(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = root / "config.json"
            cfg.write_text(
                json.dumps(
                    {
                        "email_provider": "yyds",
                        "yyds_api_key": "k",
                        "turnstile_provider": "local",
                        "turnstile_headless": True,
                        "register_count": 10,
                        "concurrent_workers": 10,
                        "local_turnstile_max_workers": 8,
                    }
                ),
                encoding="utf-8",
            )
            settings = svc.Settings(
                config_path=cfg,
                count=10,
                workers=10,
                output_dir=root / "creds",
                run_mode=svc.RUN_MODE_REGISTER_OTP,
                turnstile_provider="local",
                turnstile_headless=True,
                config=svc._read_config(cfg),
            )
            plan = svc.build_plan(settings)
            self.assertEqual(plan.workers, 8)
            self.assertTrue(any("local_turnstile_max_workers" in w for w in plan.warnings))
            # YYDS warning remains separate and must not claim the local hard-cap reason alone.
            self.assertTrue(any("YYDS" in w for w in plan.warnings))
            self.assertFalse(
                any(("限制为" in w and "YYDS" in w) for w in plan.warnings),
                msg=f"local cap warning should not mix YYDS: {plan.warnings}",
            )

    def test_build_plan_non_local_ignores_local_cap(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = root / "config.json"
            cfg.write_text(
                json.dumps(
                    {
                        "email_provider": "yyds",
                        "yyds_api_key": "k",
                        "turnstile_provider": "capsolver",
                        "turnstile_api_key": "CAP-test",
                        "register_count": 5,
                        "concurrent_workers": 5,
                        "local_turnstile_max_workers": 1,
                    }
                ),
                encoding="utf-8",
            )
            settings = svc.Settings(
                config_path=cfg,
                count=5,
                workers=5,
                output_dir=root / "creds",
                run_mode=svc.RUN_MODE_REGISTER_OTP,
                turnstile_provider="capsolver",
                turnstile_headless=False,
                config=svc._read_config(cfg),
            )
            plan = svc.build_plan(settings)
            self.assertEqual(plan.workers, 5)

    def test_resolve_local_turnstile_max_workers_defaults_and_strict(self):
        self.assertEqual(svc.resolve_local_turnstile_max_workers({}), 3)
        self.assertEqual(
            svc.resolve_local_turnstile_max_workers({"local_turnstile_max_workers": 12}),
            12,
        )
        # runtime/non-strict: bad values fall back to default
        self.assertEqual(
            svc.resolve_local_turnstile_max_workers({"local_turnstile_max_workers": 0}),
            3,
        )
        self.assertEqual(
            svc.resolve_local_turnstile_max_workers({"local_turnstile_max_workers": 7000}),
            3,
        )
        with self.assertRaises(svc.TuiConfigError):
            svc.resolve_local_turnstile_max_workers(
                {"local_turnstile_max_workers": 0},
                strict=True,
            )
        with self.assertRaises(svc.TuiConfigError):
            svc.resolve_local_turnstile_max_workers(
                {"local_turnstile_max_workers": 7000},
                strict=True,
            )
```

- [ ] **Step 2: 跑测试，确认失败**

Run:

```bash
python -m unittest tests.FAKESECRET_e4f5g6h7i8j9k0l1m2n3 tests.FAKESECRET_a4b5c6d7e8f9g0h1i2j3 tests.FAKESECRET_a4b5c6d7e8f9g0h1i2j3 -v
```

Expected: FAIL（`resolve_local_turnstile_max_workers` 不存在，或 cap 仍写死 3）

- [ ] **Step 3: 实现常量和解析函数**

在 `http_batch_service.py` 常量区改为：

```python
MAX_WORKERS = 32
MAX_LOCAL_TURNSTILE_WORKERS = 3  # default local Turnstile concurrency cap
MIN_LOCAL_TURNSTILE_WORKERS = 1
ABS_MAX_LOCAL_TURNSTILE_WORKERS = 6666
```

在 `_positive_int` 附近新增：

```python
def resolve_local_turnstile_max_workers(
    config: Optional[Dict[str, object]] = None,
    *,
    strict: bool = False,
) -> int:
    """Return configured local Turnstile worker cap.

    strict=True: invalid values raise TuiConfigError (config-center save path).
    strict=False: missing/invalid values fall back to MAX_LOCAL_TURNSTILE_WORKERS.
    """
    raw = None if not isinstance(config, dict) else config.get("local_turnstile_max_workers")
    if raw is None or str(raw).strip() == "":
        return MAX_LOCAL_TURNSTILE_WORKERS
    try:
        number = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        if strict:
            raise TuiConfigError("本地 Turnstile 并发上限必须是整数") from exc
        return MAX_LOCAL_TURNSTILE_WORKERS
    if not MIN_LOCAL_TURNSTILE_WORKERS <= number <= ABS_MAX_LOCAL_TURNSTILE_WORKERS:
        if strict:
            raise TuiConfigError(
                "本地 Turnstile 并发上限必须介于 "
                f"{MIN_LOCAL_TURNSTILE_WORKERS} 到 {ABS_MAX_LOCAL_TURNSTILE_WORKERS} 之间"
            )
        return MAX_LOCAL_TURNSTILE_WORKERS
    return number
```

- [ ] **Step 4: 改 `build_plan` local 分支**

把 local 分支里写死 `MAX_LOCAL_TURNSTILE_WORKERS` 的逻辑换成解析结果，并拆开警告文案。目标片段：

```python
    elif provider == "local":
        local_cap = resolve_local_turnstile_max_workers(config, strict=False)
        warnings.append(
            "主流程仍是 HTTP 协议；仅在 Turnstile 求解阶段临时打开本地浏览器"
            + ("（无头）" if turnstile_headless else "（有界面）")
            + "，拿完 token 立即关闭。"
        )
        if turnstile_headless:
            warnings.append(
                "本地无头会映射为 virtual-headed（Xvfb）；"
                "每个 worker 都会起浏览器，建议并发 <= "
                f"{local_cap}（配置 local_turnstile_max_workers）。"
            )
        if workers > local_cap:
            workers = local_cap
            warnings.append(
                f"本地浏览器 Turnstile 已将并发限制为 {local_cap}"
                "（配置 local_turnstile_max_workers），避免本机浏览器资源打满。"
            )
```

注意：删除旧文案里 “避免 YYDS 建邮限流和本机浏览器资源打满” 的混写；YYDS 单独警告保持在后面 `email_provider == "yyds"` 分支。

- [ ] **Step 5: 跑 Task 1 测试，确认通过**

Run:

```bash
python -m unittest tests.HttpBatchServiceSmokeTests -v
```

Expected: PASS（含旧 `test_build_plan_local_caps_workers`）

- [ ] **Step 6: Commit**

```bash
git add http_batch_service.py tests/test_http_batch_service.py
git commit -m "feat: make local Turnstile worker cap configurable in build_plan"
```

---

### Task 2: 配置中心读写 `local_turnstile_max_workers`

**Files:**
- Modify: `http_batch_service.py`（`build_config_center` fields；`BatchService.update_config_center`）
- Test: `tests/test_http_batch_service.py`

**Interfaces:**
- Consumes: `resolve_local_turnstile_max_workers(..., strict=True)`
- Produces:
  - GET fields 含 `local_turnstile_max_workers: int`
  - PUT 校验并写入 `config.json`

- [ ] **Step 1: 写失败测试**

在 `ConfigCenterTests` 新增：

```python
    def test_config_center_reads_and_writes_local_turnstile_max_workers(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = root / "config.json"
            cfg.write_text(
                json.dumps(
                    {
                        "email_provider": "yyds",
                        "yyds_api_key": "k",
                        "turnstile_provider": "local",
                        "register_count": 1,
                        "concurrent_workers": 1,
                        "local_turnstile_max_workers": 4,
                    }
                ),
                encoding="utf-8",
            )
            service = svc.BatchService(config_path=cfg, root_dir=root)
            data = service.get_config_center()
            self.assertEqual(data["fields"]["local_turnstile_max_workers"], 4)

            updated = service.update_config_center(
                {"fields": {"local_turnstile_max_workers": 9}}
            )
            self.assertEqual(updated["fields"]["local_turnstile_max_workers"], 9)
            disk = json.loads(cfg.read_text(encoding="utf-8"))
            self.assertEqual(disk["local_turnstile_max_workers"], 9)

    def test_config_center_rejects_invalid_local_turnstile_max_workers(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = root / "config.json"
            cfg.write_text(
                json.dumps(
                    {
                        "email_provider": "yyds",
                        "yyds_api_key": "k",
                        "turnstile_provider": "local",
                        "register_count": 1,
                        "concurrent_workers": 1,
                    }
                ),
                encoding="utf-8",
            )
            service = svc.BatchService(config_path=cfg, root_dir=root)
            with self.assertRaises(svc.TuiConfigError):
                service.update_config_center({"fields": {"local_turnstile_max_workers": 0}})
            with self.assertRaises(svc.TuiConfigError):
                service.update_config_center({"fields": {"local_turnstile_max_workers": 7000}})
            with self.assertRaises(svc.TuiConfigError):
                service.update_config_center({"fields": {"local_turnstile_max_workers": "abc"}})
```

- [ ] **Step 2: 跑测试，确认失败**

Run:

```bash
python -m unittest tests.FAKESECRET_m3n4o5p6q7r8s9t0u1v2 tests.FAKESECRET_y3z4a5b6c7d8e9f0g1h2 -v
```

Expected: FAIL（fields 缺 key 或 PUT 未校验）

- [ ] **Step 3: `build_config_center` 增加字段**

在 `fields` 字典中，靠近 `turnstile_headless` 加入：

```python
            "turnstile_headless": bool(settings.turnstile_headless),
            "local_turnstile_max_workers": resolve_local_turnstile_max_workers(raw, strict=False),
```

- [ ] **Step 4: `update_config_center` 严格写入**

在 `if "local_proxy_port" in fields:` 块附近增加：

```python
        if "local_turnstile_max_workers" in fields:
            cfg["local_turnstile_max_workers"] = resolve_local_turnstile_max_workers(
                {"local_turnstile_max_workers": fields.get("local_turnstile_max_workers")},
                strict=True,
            )
```

不要把它塞进 `plain_keys` 字符串列表（那会变成字符串并跳过严格整数校验）。

- [ ] **Step 5: 跑配置中心相关测试**

Run:

```bash
python -m unittest tests.ConfigCenterTests -v
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add http_batch_service.py tests/test_http_batch_service.py
git commit -m "feat: expose local_turnstile_max_workers in config center API"
```

---

### Task 3: WebUI 配置页表单

**Files:**
- Modify: `webui/templates/config.html`
- Modify: `webui/static/config.js`
- Test: `tests/test_webui_app.py`（可选页面源码断言；至少保证现有 config 页测试仍过）

**Interfaces:**
- Consumes: `/api/config-center` fields.`local_turnstile_max_workers`
- Produces: 表单可展示/提交该字段

- [ ] **Step 1: 写/扩展失败测试（页面含字段名）**

在 `tests/test_webui_app.py` 的 `test_config_center_page_and_api`（或邻近测试）增加断言：

```python
            page = client.get("/config")
            self.assertEqual(page.status_code, 200)
            self.assertIn("local_turnstile_max_workers", page.text)
```

若该测试当前结构不便改，可新增：

```python
    def test_config_page_has_local_turnstile_max_workers_field(self):
        with tempfile.TemporaryDirectory() as d:
            service = self._service(Path(d))
            app = create_app(service)  # 按现有测试里的 app 创建方式调整
            client = TestClient(app)
            page = client.get("/config")
            self.assertEqual(page.status_code, 200)
            self.assertIn('name="local_turnstile_max_workers"', page.text)
```

实现时以仓库现有 `test_webui_app.py` 的 app 构造 helper 为准，不要发明新启动方式。

- [ ] **Step 2: 跑测试，确认失败**

Run:

```bash
python -m unittest tests.test_webui_app -v
```

Expected: FAIL（页面无字段名）

- [ ] **Step 3: 改 `webui/templates/config.html`**

在 `Turnstile 无头` checkbox 后插入：

```html
        <label class="check"><input type="checkbox" name="turnstile_headless" /> Turnstile 无头</label>
        <label>本地 Turnstile 并发上限
          <input name="local_turnstile_max_workers" type="number" min="1" max="6666" value="3" />
        </label>
        <p class="muted">仅 turnstile=local 生效；总并发仍受运行台并发数与 32 上限约束</p>
```

- [ ] **Step 4: 改 `webui/static/config.js`**

`fill()` 增加：

```javascript
  set("turnstile_headless", !!f.turnstile_headless, true);
  set(
    "local_turnstile_max_workers",
    f.local_turnstile_max_workers == null ? 3 : f.local_turnstile_max_workers
  );
```

`collectFields()` 增加：

```javascript
    turnstile_headless: g("turnstile_headless", true),
    local_turnstile_max_workers: Number(g("local_turnstile_max_workers") || 3),
```

- [ ] **Step 5: 跑 WebUI 测试**

Run:

```bash
python -m unittest tests.test_webui_app -v
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add webui/templates/config.html webui/static/config.js tests/test_webui_app.py
git commit -m "feat: add local Turnstile max workers field to WebUI config center"
```

---

### Task 4: 示例配置与文档 + 回归

**Files:**
- Modify: `config.example.json`
- Modify: `USAGE.md`（若存在配置字段表，补一行；没有表就在 Turnstile 相关段落补一句）
- Test: 全量相关 unittest

- [ ] **Step 1: 更新 `config.example.json`**

在 `turnstile_provider` / `turnstile_headless` 附近加入：

```json
    "turnstile_provider": "capsolver",
    "turnstile_api_key": "",
    "turnstile_headless": false,
    "local_turnstile_max_workers": 3,
```

- [ ] **Step 2: 更新 `USAGE.md`**

在配置字段说明处补一行（位置贴近 `turnstile_*`）：

```markdown
| `local_turnstile_max_workers` | 本地浏览器 Turnstile 并发上限（默认 3，范围 1~6666；仅 `turnstile_provider=local` 生效） |
```

若 `USAGE.md` 不是表格而是列表，用同等信息的列表项。

- [ ] **Step 3: 跑相关回归测试**

Run:

```bash
python -m unittest tests.test_http_batch_service tests.test_webui_app tests.test_http_tui_launcher -v
```

Expected: PASS  
说明：`test_http_tui_launcher` 里 local cap 测试应仍通过（默认 3 语义不变）。

- [ ] **Step 4: 手动冒烟（可选但推荐）**

```bash
python - <<'PY'
import http_batch_service as svc
from pathlib import Path
import json, tempfile
with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    cfg = root / 'config.json'
    cfg.write_text(json.dumps({
        'email_provider':'yyds','yyds_api_key':'k',
        'turnstile_provider':'local','turnstile_api_key':'',
        'register_count':10,'concurrent_workers':10,
        'local_turnstile_max_workers':8,
    }), encoding='utf-8')
    s = svc.BatchService(config_path=cfg, root_dir=root).settings
    s.workers = 10
    s.turnstile_provider = 'local'
    plan = svc.build_plan(s)
    print(plan.workers, plan.warnings)
PY
```

Expected: `workers == 8`，警告含 `local_turnstile_max_workers`，且 local cap 警告不含 “YYDS 建邮限流和本机浏览器” 混写句。

- [ ] **Step 5: Commit**

```bash
git add config.example.json USAGE.md
git commit -m "docs: document local_turnstile_max_workers config"
```

---

## Spec Coverage Checklist

| Spec 要求 | 对应 Task |
|---|---|
| 字段 `local_turnstile_max_workers` 默认 3、范围 1~6666 | Task 1 + 2 |
| 仅 local 生效 | Task 1 |
| 配置中心可读写 | Task 2 + 3 |
| `build_plan` 用配置 cap | Task 1 |
| 保存严格校验 / 运行时兜底 | Task 1 + 2 |
| 警告文案拆分 | Task 1 |
| `config.example` / 文档 | Task 4 |
| 测试覆盖默认/配置/非 local/非法值 | Task 1 + 2 + 4 |
| 不放开 `MAX_WORKERS=32` | 全任务不改该常量 |

## Self-Review Notes

- 无 TBD/TODO 占位
- 函数名统一为 `resolve_local_turnstile_max_workers`
- 字段名统一为 `local_turnstile_max_workers`
- 每个 Task 都有失败测试 → 实现 → 通过 → commit
