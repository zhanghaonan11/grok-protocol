#!/usr/bin/env python3
"""
手动执行统一交互入口
对应文档：手动执行.md

运行方式：
    python manual_runner.py
    ./manual_runner.py
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"
PYTHON = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable


def clear():
    """清屏（非交互环境跳过）。"""
    if not sys.stdin.isatty():
        return
    if os.name == "nt":
        os.system("cls")
    elif os.environ.get("TERM"):
        os.system("clear")
    else:
        print("\n" * 3)


def header(title: str):
    """打印小节标题。"""
    print("\n" + "=" * 60)
    print(f" {title}")
    print("=" * 60 + "\n")


def run(cmd: list[str], *, cwd: Path = PROJECT_ROOT):
    """执行命令并等待完成。"""
    print("$ " + " ".join(cmd))
    print("-" * 60)
    try:
        subprocess.run(cmd, cwd=cwd, check=False)
    except KeyboardInterrupt:
        print("\n[!] 用户中断")
    print("\n" + "-" * 60)


def run_python_code(code: str):
    """通过当前 Python 解释器执行内联代码（代码经 stdin 传入）。"""
    print(f"$ {PYTHON} - <inline>")
    print("-" * 60)
    try:
        subprocess.run(
            [PYTHON, "-"],
            input=code.encode("utf-8"),
            cwd=PROJECT_ROOT,
            check=False,
        )
    except KeyboardInterrupt:
        print("\n[!] 用户中断")
    print("\n" + "-" * 60)


def load_config() -> dict:
    """读取 config.json，不存在则返回空字典。"""
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[!] 读取 config.json 失败: {e}")
    return {}


def input_default(prompt: str, default: str) -> str:
    """带默认值的输入。"""
    value = input(f"{prompt} [{default}]: ").strip()
    return value if value else default


def pause():
    """按任意键继续（非交互环境自动跳过）。"""
    if sys.stdin.isatty():
        input("\n按 Enter 返回主菜单...")
    else:
        print("\n[非交互环境，跳过暂停]")


def menu():
    """主菜单。"""
    clear()
    print("=" * 60)
    print(" 手动执行统一入口")
    print(f" 项目根目录: {PROJECT_ROOT}")
    print(f" Python: {PYTHON}")
    print(f" 配置: {CONFIG_PATH} {'(存在)' if CONFIG_PATH.exists() else '(缺失)'}")
    print("=" * 60)
    print("\n【注册】")
    print("  1. 注册（最短路径 / 读 config.json）")
    print("  2. 注册（带代理 + captcha 服务）")
    print("  3. TUI 批量注册")
    print("  4. 探测邮箱（不注册）")
    print("\n【转换】")
    print("  5. SSO 文件 → JSON（auth 换票）")
    print("  6. 单条 SSO → JSON")
    print("  7. 补「有账号行、缺 JSON」")
    print("  8. 检查转换结果")
    print("  9. 提取所有 SSO 到 all_sso.txt")
    print("\n【上传】")
    print(" 10. 检查 CPA 配置")
    print(" 11. 上传本地 JSON 到 CPA（去重）")
    print(" 12. 上传本地 JSON 到 CPA（强制覆盖）")
    print("\n【其他】")
    print(" 13. 启动 WebUI")
    print(" 14. 帮助入口")
    print("\n  0. 退出")
    print("=" * 60)


def action_register_simple():
    """1. 注册最短路径。"""
    header("注册：最短路径")
    output = input_default("账号行输出文件", "accounts_manual.txt")
    cmd = [
        PYTHON,
        "grok_register_ttk.py",
        "http",
        "register",
        "--mail-config",
        "config.json",
        "--output-dir",
        "xai_credentials",
        "--accounts-output",
        output,
    ]
    run(cmd)
    pause()


def action_register_proxy():
    """2. 注册（带代理 + captcha）。"""
    header("注册：带代理 + captcha")
    proxy_file = input_default("代理文件", "proxies.txt")
    proxy_parent = input_default("父代理 (留空表示不用)", "")
    provider = input_default("captcha 服务商", "capsolver")
    api_key = input("captcha API Key: ").strip()
    output = input_default("账号行输出文件", "accounts_manual.txt")

    cmd = [
        PYTHON,
        "grok_register_ttk.py",
        "http",
        "register",
        "--proxy-file",
        proxy_file,
        "--proxy-random",
        "--mail-config",
        "config.json",
        "--turnstile-provider",
        provider,
        "--turnstile-api-key",
        api_key or "YOUR_KEY",
        "--output-dir",
        "xai_credentials",
        "--accounts-output",
        output,
    ]
    if proxy_parent:
        cmd += ["--proxy-parent", proxy_parent]
    run(cmd)
    pause()


def action_tui():
    """3. TUI 批量注册。"""
    header("TUI 批量注册")
    config = input_default("配置文件", "config.json")
    count = input_default("注册数量", "5")
    workers = input_default("并发数", "2")
    run(["./http_tui.sh", "--config", config, "--count", count, "--workers", workers])
    pause()


def action_mail_probe():
    """4. 探测邮箱。"""
    header("探测邮箱")
    run([PYTHON, "grok_register_ttk.py", "http", "mail-probe", "--mail-config", "config.json"])
    pause()


def action_convert_file():
    """5. SSO 文件 → JSON。"""
    header("SSO 文件 → JSON（auth 换票）")
    sso_file = input_default("SSO/账号行文件", "accounts_manual.txt")
    out_dir = input_default("输出目录", "./xai_credentials")
    workers = input_default("并发数", "8")
    run([
        PYTHON, "sso_to_auth_json.py",
        "--mode", "auth",
        "--sso", sso_file,
        "--out-dir", out_dir,
        "--workers", workers,
    ])
    pause()


def action_convert_single():
    """6. 单条 SSO → JSON。"""
    header("单条 SSO → JSON")
    sso = input("SSO cookie: ").strip()
    email = input("邮箱: ").strip()
    out_dir = input_default("输出目录", "./xai_credentials")
    if not sso or not email:
        print("[!] SSO 和邮箱不能为空")
        pause()
        return
    run([
        PYTHON, "sso_to_auth_json.py",
        "--mode", "auth",
        "--sso-cookie", sso,
        "--email", email,
        "--out-dir", out_dir,
    ])
    pause()


def action_convert_missing():
    """7. 补「有账号行、缺 JSON」。"""
    header("补全缺 JSON 的账号")
    code = r'''
from pathlib import Path
import glob, json
import sso_to_auth_json as conv

OUT = Path("xai_credentials")
OUT.mkdir(parents=True, exist_ok=True)

json_emails = set()
for p in OUT.glob("*.json"):
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        e = str(d.get("email", "")).strip().lower()
        if e:
            json_emails.add(e)
    except Exception:
        pass

best = {}
for path in sorted(set(glob.glob("accounts_*.txt") + glob.glob("accounts_http_*.txt"))):
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if "----" not in line:
            continue
        parts = line.split("----")
        if len(parts) < 3:
            continue
        email = parts[0].strip().lower()
        sso = parts[-1].strip()
        if not email or not sso or email in json_emails:
            continue
        prev = best.get(email)
        if prev is None or len(sso) >= len(prev):
            best[email] = sso

print(f"待转换: {len(best)}")
ok = fail = 0
items = list(best.items())
for i, (email, sso) in enumerate(items, 1):
    print(f"[{i}/{len(items)}] {email}")
    if conv.process_auth_one(i, len(items), sso, OUT, email):
        ok += 1
    else:
        fail += 1
print(f"完成: 成功 {ok}, 失败 {fail}")
'''
    run_python_code(code)
    pause()


def action_check_results():
    """8. 检查转换结果。"""
    header("检查转换结果")
    code = r'''
from pathlib import Path
import glob, json, re

emails = set()
for p in glob.glob("accounts_*.txt") + glob.glob("accounts_http_*.txt"):
    for line in Path(p).read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", line)
        emails.update(x.lower() for x in m)

json_emails = set()
for p in Path("xai_credentials").glob("*.json"):
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        e = str(d.get("email", "")).strip().lower()
        if e:
            json_emails.add(e)
    except Exception:
        pass

print("账号邮箱:", len(emails))
print("JSON 邮箱:", len(json_emails))
print("缺 JSON:", len(emails - json_emails))
for e in sorted(emails - json_emails):
    print(" ", e)
'''
    run_python_code(code)
    pause()


def action_extract_all_sso():
    """9. 提取所有 SSO 到 all_sso.txt。"""
    header("提取所有 SSO 到 all_sso.txt")
    code = r'''
from pathlib import Path
import glob

out = Path("all_sso.txt")
seen = set()
with out.open("w", encoding="utf-8") as f:
    for path in sorted(set(glob.glob("accounts_*.txt") + glob.glob("accounts_http_*.txt"))):
        for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.strip().split("----")
            if len(parts) >= 3:
                sso = parts[-1].strip()
                if sso and sso not in seen:
                    seen.add(sso)
                    f.write(sso + "\n")

    for p in sorted(Path("xai_credentials").glob("*.sso")):
        sso = p.read_text(encoding="utf-8", errors="replace").strip()
        if sso and sso not in seen:
            seen.add(sso)
            f.write(sso + "\n")

print(f"已保存 {len(seen)} 条唯一 SSO 到 {out}")
'''
    run_python_code(code)
    pause()


def action_check_cpa():
    """10. 检查 CPA 配置。"""
    header("检查 CPA 配置")
    if not CONFIG_PATH.exists():
        print("[!] config.json 不存在")
        pause()
        return
    code = r'''
import json
import cpa_push

cfg = json.load(open("config.json", encoding="utf-8"))
print("url:", cpa_push.normalize_cpa_base_url(cfg.get("cpa_api_url")))
print("key:", cpa_push.mask_secret(cfg.get("cpa_api_key")))
print("auto:", cfg.get("cpa_auto_upload"))
print("skip_dup:", cfg.get("cpa_skip_duplicates"))
print("use_local_name:", cfg.get("cpa_use_local_name"))
print("dir:", cfg.get("xai_oauth_output_dir", "xai_credentials"))
print(cpa_push.check_cpa_connection(cfg["cpa_api_url"], cfg["cpa_api_key"])["message"])
'''
    run_python_code(code)
    pause()


def action_push_cpa(skip_duplicates: bool = True):
    """11/12. 上传本地 JSON 到 CPA。"""
    header("上传本地 JSON 到 CPA" + ("（去重）" if skip_duplicates else "（强制覆盖）"))
    if not CONFIG_PATH.exists():
        print("[!] config.json 不存在")
        pause()
        return
    code = rf'''
import json
import cpa_push

cfg = json.load(open("config.json", encoding="utf-8"))
result = cpa_push.push_local_credentials(
    base_url=cfg["cpa_api_url"],
    api_key=cfg["cpa_api_key"],
    output_dir=cfg.get("xai_oauth_output_dir", "xai_credentials"),
    use_local_name=bool(cfg.get("cpa_use_local_name", True)),
    skip_duplicates={str(skip_duplicates)},
    log=print,
)
print(result.get("message"))
print(
    f"total={{result.get('total')}} "
    f"success={{result.get('success')}} "
    f"failed={{result.get('failed')}} "
    f"skipped={{result.get('skipped')}}"
)
'''
    run_python_code(code)
    pause()


def action_webui():
    """13. 启动 WebUI。"""
    header("启动 WebUI")
    run(["./webui.sh"])
    pause()


def action_help():
    """14. 帮助入口。"""
    header("帮助入口")
    print("【grok_register_ttk.py】")
    run([PYTHON, "grok_register_ttk.py", "http", "--help"])
    print("\n【sso_to_auth_json.py】")
    run([PYTHON, "sso_to_auth_json.py", "--help"])
    pause()


def main():
    """主循环。"""
    actions = {
        "1": action_register_simple,
        "2": action_register_proxy,
        "3": action_tui,
        "4": action_mail_probe,
        "5": action_convert_file,
        "6": action_convert_single,
        "7": action_convert_missing,
        "8": action_check_results,
        "9": action_extract_all_sso,
        "10": action_check_cpa,
        "11": lambda: action_push_cpa(skip_duplicates=True),
        "12": lambda: action_push_cpa(skip_duplicates=False),
        "13": action_webui,
        "14": action_help,
    }

    while True:
        menu()
        choice = input("请输入选项编号: ").strip()
        if choice == "0":
            print("再见。")
            break
        action = actions.get(choice)
        if action is None:
            print("[!] 无效选项，请重新输入")
            pause()
        else:
            try:
                action()
            except Exception as e:
                print(f"[!] 执行出错: {e}")
                pause()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n已退出。")
        sys.exit(0)
