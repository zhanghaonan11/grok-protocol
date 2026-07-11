from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from .config import load_config
from .models import SolveRequest
from .service import SolverService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local Turnstile token factory")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Start HTTP API")
    serve.add_argument("--config", default="", help="Path to solver config JSON")
    serve.add_argument("--host", default="", help="Override bind host")
    serve.add_argument("--port", type=int, default=0, help="Override bind port")

    solve = sub.add_parser("solve", help="Capture one Turnstile token via local Chrome")
    solve.add_argument("--config", default="", help="Path to solver config JSON")
    solve.add_argument("--proxy", default="", help="Proxy URL/string for this task")
    solve.add_argument(
        "--parent-proxy",
        default="",
        help="Optional parent proxy (e.g. Clash http://127.0.0.1:7890)",
    )
    solve.add_argument("--page-url", default="", help="Signup page URL")
    solve.add_argument("--timeout-sec", type=int, default=0)
    solve.add_argument("--headless", action="store_true")
    solve.add_argument("--user-agent", default="", help="Optional browser UA override")
    solve.add_argument(
        "--output",
        default="",
        help="Optional path to write token (utf-8). Also prints JSON to stdout.",
    )
    solve.add_argument(
        "--proxy-used-file",
        default="",
        help="Optional path to write the upstream proxy that should be reused for register",
    )
    solve.add_argument(
        "--no-diagnose",
        action="store_true",
        help="Disable page diagnostics (enabled by default)",
    )

    health = sub.add_parser("health", help="Print local service health JSON")
    health.add_argument("--config", default="", help="Path to solver config JSON")
    return parser


def _write_text(path: str, content: str) -> None:
    import os

    out = Path(path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content, encoding="utf-8")
    try:
        if os.name != "nt":
            out.chmod(0o600)
    except OSError:
        pass


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.config or None)
    if getattr(args, "host", ""):
        config.host = args.host
    if getattr(args, "port", 0):
        config.port = int(args.port)

    service = SolverService(config)

    if args.command == "health":
        print(json.dumps(service.health(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "solve":
        parent_proxy = args.parent_proxy or config.parent_proxy
        result = service.solve(
            SolveRequest(
                proxy=args.proxy or config.proxy,
                page_url=args.page_url,
                timeout_sec=args.timeout_sec or config.browser_timeout_sec,
                headless=bool(args.headless or config.headless),
                user_agent=args.user_agent or config.user_agent,
                metadata={"parent_proxy": parent_proxy, "diagnose": (not args.no_diagnose)},
            )
        )
        if result.ok and args.output:
            _write_text(args.output, result.token + "\n")
        if args.proxy_used_file:
            _write_text(args.proxy_used_file, (result.proxy or args.proxy or "") + "\n")
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0 if result.ok else 2

    if args.command == "serve":
        try:
            import uvicorn
            from .api import create_app
        except Exception as exc:  # pragma: no cover
            print(
                f"serve dependencies missing: {exc}\n"
                "Install: pip install -r turnstile_solver/requirements.txt",
                file=sys.stderr,
            )
            return 1
        app = create_app(service)
        uvicorn.run(app, host=config.host, port=config.port, log_level="info")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
