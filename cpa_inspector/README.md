# CPA Web 巡检台（已合并进 grok 协议项目）

本机浏览器可用的 CLIProxyAPI 凭证巡检工具：连接远端、筛选分页查阅号池、导入/导出/导出并删除，以及健康探测与配置持久化。

## 推荐启动（统一 WebUI）

```bash
cd "/home/scv/nvme0n1p1/注册机相关/grok协议-es1/grok协议"
./webui.sh
```

打开：http://127.0.0.1:33844/cpa

API 前缀：`/api/cpa/*`  
静态资源：`/static/cpa/*`

## 独立启动（可选）

```bash
python cpa_main.py
# http://127.0.0.1:8218  →  自动跳转到 /cpa
```

## 功能

- 远程连接
- 号池筛选分页
- 导入 / 导出 / 导出并删除
- 健康探测
- 配置持久化（`~/.config/cpa_web_inspector/`）

## 测试

```bash
python -m unittest discover -s tests -p 'test_cpa_*.py' -v
```
