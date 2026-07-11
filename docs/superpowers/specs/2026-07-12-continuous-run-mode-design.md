# 持续运行模式（无限补货）设计

日期：2026-07-12  
状态：已评审通过（待实现）  
范围：xAI HTTP 注册机 WebUI / BatchRunner

## 1. 背景与问题

当前批次调度在启动时按 `register_count` **预创建全部** `WorkerState`：

```python
self.workers = [WorkerState(index=i) for i in range(1, plan.count + 1)]
```

当用户填入超大值（例如 `128000`）时会出现：

1. 内存与对象数量一次性膨胀
2. 进度/停止/统计需要扫描超大列表
3. 失败数随真实失败持续累计，观感极差
4. 用户真正想要的是“一直跑”，不是“先建 12.8 万任务表”

结论：无限跑不应再复用超大 `count` 预分配模型。

## 2. 目标

### 2.1 必须支持

1. **固定数量模式**（兼容现状）
2. **持续运行 + 仅手动停止**
3. **持续运行 + 成功 N 个后自动停止**
4. 持续模式下内存占用与并发大致成正比，不随“目标总量”线性膨胀
5. 停止时不把“未启动任务”计为失败

### 2.2 非目标（本轮不做）

1. 多机分布式调度
2. 失败任务自动无限重试直到成功
3. 修改 xAI 注册协议/Turnstile 求解算法本身
4. 完全重写 WebUI

## 3. 用户可见行为

### 3.1 运行台新增

| 字段 | 类型 | 说明 | 默认 |
|---|---|---|---|
| `run_target_mode` | `count` / `continuous` | 目标模式 | `count` |
| `target_success` | int >= 0 | 持续模式成功目标；`0` 表示不限，仅手动停 | `0` |
| `continuous_max_runtime_min` | int >= 0 | 可选最长运行分钟；`0` 表示不限 | `0` |

固定模式继续使用现有 `register_count`。  
持续模式 **忽略** `register_count` 作为预创建总量，只使用并发水位线。

### 3.2 进度展示

固定模式：

```text
完成 completed/count | 成功 S | 失败 F | 活动 A
```

持续模式：

```text
已启动 started | 成功 S[/target] | 失败 F | 活动 A | 模式=持续
```

若 `target_success > 0`，成功位显示 `S/target`；否则显示 `S/∞`（前端可用“不限”文案）。

### 3.3 停止按钮

1. 进入 `draining`（排水/收尾）
2. 停止补新任务
3. 等待当前 `running/converting` 结束
4. 全部清空后 `done`

二次停止可保留现有强制 terminate 行为（若已有）。

## 4. 调度设计

### 4.1 核心模型：水位线补货

```text
start
  -> ensure broker / embedded proxy
  -> phase = running
  -> while phase == running:
        spawn until active == concurrent_workers
  -> on worker terminal (success/fail):
        update counters
        if should_continue(): spawn one more
        else phase = draining
  -> when active == 0: finalize -> done
```

`should_continue()` 为真当且仅当：

1. 未手动停止
2. 未触发熔断 pause
3. 若 `target_success > 0`，则 `succeeded < target_success`
4. 若 `continuous_max_runtime_min > 0`，则未超时

### 4.2 数据结构

`RunPlan` 增加：

- `target_mode: str` (`count` | `continuous`)
- `target_success: int`
- `continuous_max_runtime_min: int`

`BatchRunner` 在持续模式下：

- **不再** `workers = [ ... ] * count`
- 使用：
  - `next_index: int` 自增任务序号
  - `active_workers: dict[int, WorkerState]`
  - `recent_workers: deque[WorkerState]`（UI 窗口，默认 200）
  - 计数器：`started/succeeded/failed/active`

固定数量模式可继续预创建，或同样改为“按需创建但上限=count”。  
为减少双路径风险，**推荐两种模式都改为按需创建**：

- `count` 模式：`started` 达到 `count` 后停止补货
- `continuous` 模式：按 `should_continue()` 补货

### 4.3 成功/失败定义（保持兼容）

- 模式1：协议进程 exit 0 => succeeded；非 0 => failed
- 模式2：协议 exit 0 后进入 converting；SSO 转换成功 => succeeded，否则 failed
- `stopped` 仅表示操作者打断/未完成收尾，**不计入 failed，不计入 completed**
- `completed = succeeded + failed`

### 4.4 快照 API

`snapshot()` 输出至少包含：

```json
{
  "run_id": "...",
  "target_mode": "continuous",
  "count": 0,
  "target_success": 5000,
  "started": 1234,
  "completed": 1200,
  "succeeded": 900,
  "failed": 300,
  "stopped": 0,
  "active": 16,
  "phase": "running|draining|done",
  "workers": [ /* 最近窗口，已 slim */ ],
  "worker_total": 16,
  "workers_truncated": 0,
  "failure_counts": {},
  "pause_reason": ""
}
```

说明：

- 固定模式下 `count` 仍为计划总量；持续模式 `count` 可为 `0` 或与 `target_success` 展示解耦
- 前端持续模式优先显示 `started/target_success`，不要显示 `completed/128000`

## 5. 熔断与健康保护

持续模式必须防止“代理已死还狂开失败单”。

### 5.1 自动暂停补货（phase 保持 running，但 `refill_paused=true`）

触发条件（任一）：

1. 内嵌代理进程死亡/本地端口大面积不可用
2. 近窗失败率过高：默认近 `50` 个已终结任务中失败率 `>= 0.8`
3. 邮箱域名池可用域为空（全被拒且无候选）

行为：

1. 停止补新任务
2. `pause_reason` 写入快照与日志
3. 当前活跃任务继续跑完
4. WebUI 显示“已暂停补货：原因”

恢复：

- 手动“继续补货”按钮（可二期）
- 或代理恢复健康后自动恢复（本轮建议：先做自动恢复检测，每 15s 探活）

### 5.2 域名拒绝

沿用已实现逻辑：

1. xAI 返回 domain rejected
2. 拉黑域名
3. 换号重试（有限次）

持续模式下若连续换号仍失败，计该任务 failed，不拖死调度器。

## 6. WebUI / 配置中心

### 6.1 运行台

1. 目标模式切换：`固定数量` / `持续运行`
2. 持续运行时显示：
   - `target_success`（0=不限）
   - 可选 `continuous_max_runtime_min`
3. 隐藏或弱化超大 `register_count` 的误导（持续模式下禁用输入）

### 6.2 配置持久化

写入 `config.json`：

- `run_target_mode`
- `target_success`
- `continuous_max_runtime_min`

热更新策略：

- 未运行：立即生效
- 运行中：仅影响展示/下次批次；不动态改当前 runner 目标（避免歧义）

## 7. 兼容与迁移

1. 未配置新字段时默认 `run_target_mode=count`，行为与现网一致
2. 旧前端若忽略新字段，仍可读取 `succeeded/failed/active`
3. 历史 summary 缺少新字段时前端按 0/空处理

## 8. 测试计划

### 8.1 单元/集成

1. 持续模式启动时 **不** 预创建上千 WorkerState
2. `target_success=3`：成功到 3 后进入 draining，不再 spawn
3. 手动 stop：立即停止补货，活跃清零后 done
4. 固定数量模式回归：`count=5, workers=2` 恰好跑 5 个
5. `failed` 不含 `stopped`
6. 近窗失败熔断触发后不再 spawn

### 8.2 手工验收

1. 持续模式跑 10~20 分钟，内存不明显随时间线性暴涨
2. 成功目标自动停
3. 手动停后失败数不因“未启动队列”暴涨
4. 代理被杀掉后停止补货并有明确提示

## 9. 实现分期

### P0（本轮实现）

1. BatchRunner 按需 spawn + 计数器
2. `count` / `continuous` 双模式
3. `target_success` + 手动 stop
4. 快照/WebUI 展示
5. 基础测试

### P1

1. 近窗失败熔断
2. 代理死亡自动暂停/恢复
3. 配置中心完整表单项与说明

### P2

1. 运行中“继续补货”按钮
2. 失败重试队列（可选）

## 10. 风险与决策

| 风险 | 缓解 |
|---|---|
| 双路径调度复杂度 | 固定/持续都走按需 spawn，仅停止条件不同 |
| UI 仍按 count 展示导致误解 | 持续模式专用文案 |
| 长跑文件过多 | worker 日志仍按序号写；可后续做日志保留策略 |
| 成功目标与模式2 SSO 延迟 | 成功以最终 succeeded 为准，converting 占 active 槽位 |

## 11. 验收标准（DoD）

1. 可持续运行直到手动停止，或成功数达到目标
2. 持续模式内存与活跃并发相关，不随“想象中的总数”预分配
3. 失败计数只含真实失败
4. 固定数量模式不回归
5. 关键测试通过

