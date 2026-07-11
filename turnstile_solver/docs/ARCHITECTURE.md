# 架构说明

一句话：专用 Turnstile token 工厂，服务 xAI HTTP 注册，不走 FlareSolverr 深二开。

## 组件

```text
Client / xai_http_flow
        |
        v
   API (FastAPI)
        |
        v
   Service 调度
        |
        +-- Proxy binder（任务级粘性出口）
        +-- Browser pool（上限 N）
        +-- Browser worker（打开注册页 -> 读 token）
```

## 关键约束

1. `solve(proxy)` 与后续 `register(proxy)` 使用同一出口
2. token 获取后立刻返回，不做长缓存
3. worker 超时必须杀浏览器回收资源
4. 并发从 2 -> 5 -> 10 阶梯上升

## 接口草案

### `GET /health`

返回进程与池状态。

### `POST /v1/solve`

请求：

```json
{
  "proxy": "http://user:pass@host:port",
  "page_url": "https://accounts.x.ai/sign-up?redirect=grok-com",
  "timeout_sec": 180,
  "headless": false
}
```

响应：

```json
{
  "ok": true,
  "token": "<turnstile token>",
  "proxy": "http://user:pass@host:port",
  "page_url": "https://accounts.x.ai/sign-up?redirect=grok-com",
  "elapsed_ms": 12345,
  "user_agent": "..."
}
```

## 与父项目关系

- 可先独立开发本目录
- 打通后由 `xai_http_flow.solve_turnstile_token()` 增加 `local` provider
- 或注册前先调本服务，再把 token 以 `--turnstile-token` 传入
