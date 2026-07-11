# 本地 Turnstile 并发上限可配置（WebUI）设计

## 1. 背景

一句话：`turnstile_provider=local` 时，并发被代码写死压到 3，WebUI 改不了。

当前逻辑在 `http_batch_service.py`：

- 常量 `MAX_LOCAL_TURNSTILE_WORKERS = 3`
- `build_plan()` 在 `provider == "local"` 且 `workers > 3` 时强制 `workers = 3`
- 警告文案混写了“YYDS 建邮限流”和“本机浏览器资源”，容易误判

用户需要：在 WebUI 配置中心暴露该上限，默认保持 3，允许按机器能力调大。

## 2. 目标与非目标

### 目标

- 新增可持久化配置项 `local_turnstile_max_workers`
- WebUI 配置中心可查看 / 修改 / 保存
- `build_plan()` 用配置值替代写死的 3
- 默认行为与现状一致：未配置时仍按 3 限制
- 补齐读写与 cap 行为测试

### 非目标

- 不改 YYDS 建邮间隔逻辑（仍是跨进程锁 + 默认 1.5s）
- 不放开总并发硬顶 `MAX_WORKERS = 32`
- 不改远程 captcha（capsolver / 2captcha / yescaptcha）的并发策略
- 不在本需求中重做运行台布局或代理池

## 3. 配置契约

| 项 | 值 |
|---|---|
| 字段名 | `local_turnstile_max_workers` |
| 类型 | 正整数 |
| 默认 | `3` |
| 最小值 | `1` |
| 最大值 | `6666` |
| 作用域 | 仅 `turnstile_provider == "local"` |
| 存储 | `config.json` |
| 生效时机 | 下次 `build_plan()` / 启动批次时读取当前 settings |

### 实际并发计算公式

```text
effective_workers =
  min(
    requested_workers,
    register_count,
    MAX_WORKERS,                      # 32
    local_turnstile_max_workers       # 仅 local 时参与
  )
```

说明：

- 字段最大值允许填到 6666，但总并发仍受 `MAX_WORKERS=32` 约束
- 因此 local 场景真实可开并发上限是 `min(local_turnstile_max_workers, 32, count)`
- 这是刻意保留的安全顶，避免单批次无界起进程

## 4. 行为规则

### 4.1 读取

优先级：

1. `config.json` 中的 `local_turnstile_max_workers`
2. 缺省 / 空 / 非法 → 回落默认 `3`

校验：

- 非整数 / `<=0` / 超过 `6666`：在配置中心保存路径拒绝并返回明确错误；在 `build_plan` 兜底路径夹紧到合法区间并写警告

推荐分工：

- **配置中心保存**：严格校验，非法直接失败（避免脏配置落盘）
- **build_plan 运行时**：防御性夹紧，保证旧配置或手工改坏的文件仍可跑

### 4.2 local 限制

当 `turnstile_provider == "local"`：

- 若 `workers > local_turnstile_max_workers`：
  - `workers = local_turnstile_max_workers`
  - 追加警告：`本地浏览器 Turnstile 已将并发限制为 {cap}（配置 local_turnstile_max_workers）`
- 若 `turnstile_headless`：
  - 保留 virtual-headed 提示，文案改为引用配置 cap，不再写死 3

### 4.3 非 local

- `local_turnstile_max_workers` 可保存，但**不参与**并发压缩
- 不额外弹“因 local cap 被限制”的警告

### 4.4 YYDS 警告拆分

保留 YYDS 独立警告（仅提示排队/429），不再与 local cap 文案混写：

- local cap 警告：只谈本机浏览器资源与配置 cap
- yyds 警告：只谈跨进程建邮限流与间隔

## 5. 改动面

### 5.1 后端 `http_batch_service.py`

- 保留 `MAX_LOCAL_TURNSTILE_WORKERS = 3` 作为**默认值常量**（兼容旧测试名）
- 新增常量：
  - `MIN_LOCAL_TURNSTILE_WORKERS = 1`
  - `ABS_MAX_LOCAL_TURNSTILE_WORKERS = 6666`
- 新增解析函数，例如 `_local_turnstile_max_workers(config) -> int`
- `build_plan()` 用解析结果替代硬编码 3
- `get_config_center()` / `update_config_center()` 读写该字段
- `example` / 默认字段映射如有白名单，补上该 key

### 5.2 WebUI 配置中心

文件：

- `webui/templates/config.html`
- `webui/static/config.js`

UI 位置：`邮箱 / Turnstile` 区块，`Turnstile 无头` 附近

控件：

```html
<label>本地 Turnstile 并发上限
  <input name="local_turnstile_max_workers" type="number" min="1" max="6666" value="3" />
</label>
<p class="muted">仅 turnstile=local 生效；总并发仍受运行台并发数与 32 上限约束</p>
```

JS：

- `fill()` 读入
- `collectFields()` 写出数字

### 5.3 运行台

可选增强（本需求建议做最小版）：

- 运行台首页**不强制**新增输入框
- 但开始批次后的 plan 警告 / 日志要能看到实际 cap
- 若现有 `public_settings` 会回显 config，则带上该字段即可

### 5.4 文档

- `config.example.json` 增加示例字段
- README / USAGE 若有配置表，补一行说明

## 6. API 契约

### GET `/api/config-center`

`fields.local_turnstile_max_workers: number`

### PUT `/api/config-center`

请求可含：

```json
{
  "fields": {
    "local_turnstile_max_workers": 8
  }
}
```

非法示例：

- `0` / `-1` / `"abc"` / `7000` → `400`，detail 说明合法范围 `1~6666`

## 7. 测试计划

至少覆盖：

1. **默认 cap**
   - 无该字段 + local + workers=10 → plan.workers <= 3
2. **配置 cap 生效**
   - `local_turnstile_max_workers=8` + local + workers=10 → plan.workers == 8
3. **非 local 不受影响**
   - cap=1 + capsolver + workers=5 → plan.workers == 5（key 等前置满足时）
4. **配置中心读写**
   - GET 可见字段；PUT 写入后磁盘 config 含新值
5. **非法值拒绝**
   - PUT `0` 或 `7000` 失败

现有 `test_build_plan_local_caps_workers` 继续通过（默认 3 语义不变）。

## 8. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 用户把 cap 调很大导致本机 Chrome/Xvfb 打满 | 保留 `MAX_WORKERS=32`；UI 提示 local 成本 |
| 旧配置无该字段 | 默认 3，行为兼容 |
| 文案再误导 | 拆分 YYDS / local 警告 |

回滚：删除字段读取逻辑，恢复常量 3 即可；配置文件多出来的 key 无害。

## 9. 验收标准（DoD）

- [ ] WebUI 配置中心可设置 `local_turnstile_max_workers`
- [ ] 保存后写入 `config.json`
- [ ] local 模式下 `build_plan` 按该值压并发
- [ ] 默认仍为 3
- [ ] 合法范围 `1~6666`
- [ ] 相关单测通过
- [ ] 警告文案不再把 YYDS 与 local cap 绑成一句

## 10. 实现顺序建议

1. service 解析 + `build_plan` 改造 + 单测
2. config-center 读写
3. WebUI 表单
4. example/文档
5. 全量相关测试
