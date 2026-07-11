# 开发路线

## Phase 0
- 创建目录与脚手架
- 固定架构与接口草案

## Phase 1
- [x] 实现单任务 CLI：打开注册页并捕获 token
- [x] 对齐父项目 `turnstile-capture` / DrissionPage 行为
- [x] 认证代理走 `local_proxy_forwarder`
- [ ] 同代理手工接一次 `http register`（联调项）

## Phase 2
- 加上稳定 `POST /v1/solve` HTTP API 运行验证
- 加入更完整超时、基础日志、health

## Phase 3
- browser pool 强化
- max_concurrency = 2/5/10
- 代理绑定与失败隔离

## Phase 4
- 与 `xai_http_flow` 正式对接
- 本地 provider / 失败回落打码平台

## 完成标准（DoD）
- 1 并发同代理注册可用
- 2 并发不互相拖死
- 10 并发有资源上限与熔断，不把机器打挂
