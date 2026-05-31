#!/usr/bin/env python3
import argparse
import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Tuple

SAFE_METHODS = {"get", "head"}


def load_dotenv(dotenv_path: Path) -> None:
    if not dotenv_path.exists():
        return

    for raw_line in dotenv_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value and ((value[0] == value[-1]) and value[0] in ('"', "'")):
            value = value[1:-1]
        os.environ.setdefault(key, value)


def substitute_path_params(path: str) -> str:
    # Conservative deterministic defaults; can be overridden in future with fixtures.
    def repl(match: re.Match[str]) -> str:
        name = match.group(1).lower()
        if "id" in name:
            return "1"
        if "name" in name:
            return "general"
        return "1"

    return re.sub(r"\{([^}]+)\}", repl, path)


def build_auth_header(args: argparse.Namespace) -> Tuple[Dict[str, str], str]:
    headers: Dict[str, str] = {}

    auth_mode = args.auth_mode
    if auth_mode == "auto":
        if args.token or os.environ.get("RT_TOKEN", ""):
            auth_mode = "token"
        elif (args.user or os.environ.get("RT_USERNAME", "")) and (
            args.password or os.environ.get("RT_PASSWORD", "")
        ):
            auth_mode = "basic"
        elif args.cookie or os.environ.get("RT_COOKIE", ""):
            auth_mode = "cookie"
        else:
            auth_mode = "none"

    if auth_mode == "none":
        return headers, auth_mode

    if auth_mode == "token":
        token = args.token or os.environ.get("RT_TOKEN", "")
        if token:
            headers["Authorization"] = f"token {token}"
        return headers, auth_mode

    if auth_mode == "basic":
        user = args.user or os.environ.get("RT_USERNAME", "")
        password = args.password or os.environ.get("RT_PASSWORD", "")
        if user or password:
            raw = f"{user}:{password}".encode("utf-8")
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        return headers, auth_mode

    if auth_mode == "cookie":
        cookie = args.cookie or os.environ.get("RT_COOKIE", "")
        if cookie:
            headers["Cookie"] = cookie
        return headers, auth_mode

    return headers, auth_mode


def status_group(code: int) -> str:
    if code < 0:
        return "transport"
    return f"{code // 100}xx"


def collect_probe_targets(openapi: Dict[str, object], max_ops: int) -> List[Tuple[str, str, str]]:
    targets: List[Tuple[str, str, str]] = []
    paths = openapi.get("paths", {})
    if not isinstance(paths, dict):
        return targets

    for path, path_item in sorted(paths.items()):
        if not isinstance(path_item, dict):
            continue
        for method in ("get", "head"):
            op = path_item.get(method)
            if isinstance(op, dict):
                operation_id = op.get("operationId", f"{method}_{path}")
                if not isinstance(operation_id, str):
                    operation_id = f"{method}_{path}"
                targets.append((method.upper(), str(path), operation_id))
                if len(targets) >= max_ops:
                    return targets
    return targets


def probe(args: argparse.Namespace) -> Dict[str, object]:
    workspace = Path(args.workspace).resolve()
    load_dotenv(workspace / ".env")
    openapi_path = workspace / "spec" / "openapi.json"
    if not openapi_path.exists():
        raise SystemExit(f"Missing OpenAPI file: {openapi_path}")

    openapi = json.loads(openapi_path.read_text(encoding="utf-8"))
    headers, effective_auth_mode = build_auth_header(args)
    headers.setdefault("Accept", "application/json")

    targets = collect_probe_targets(openapi, args.max_ops)
    base_url = args.base_url.rstrip("/")
    if args.base_url == "http://localhost":
        base_url = os.environ.get("RT_SERVER", "") or base_url
        base_url = base_url.rstrip("/")

    results = []
    started = time.time()

    for method, path, operation_id in targets:
        resolved = substitute_path_params(path)
        url = f"{base_url}/REST/2.0{resolved}"

        request = urllib.request.Request(url=url, method=method, headers=headers)

        status = -1
        error = ""
        elapsed_ms = 0
        try:
            t0 = time.time()
            with urllib.request.urlopen(request, timeout=args.timeout) as resp:
                status = int(resp.status)
            elapsed_ms = int((time.time() - t0) * 1000)
        except urllib.error.HTTPError as http_err:
            status = int(http_err.code)
            elapsed_ms = int((time.time() - t0) * 1000)
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
            elapsed_ms = int((time.time() - t0) * 1000)

        results.append(
            {
                "operation_id": operation_id,
                "method": method,
                "path_template": path,
                "path_resolved": resolved,
                "url": url,
                "status": status,
                "status_group": status_group(status),
                "elapsed_ms": elapsed_ms,
                "error": error,
            }
        )

    finished = time.time()
    by_group: Dict[str, int] = {}
    for item in results:
        group = item["status_group"]
        by_group[group] = by_group.get(group, 0) + 1

    return {
        "meta": {
            "base_url": base_url,
            "max_ops": args.max_ops,
            "timeout_seconds": args.timeout,
            "auth_mode": args.auth_mode,
            "effective_auth_mode": effective_auth_mode,
            "auth_header_present": bool(headers.get("Authorization") or headers.get("Cookie")),
            "started_at_epoch": int(started),
            "finished_at_epoch": int(finished),
            "duration_ms": int((finished - started) * 1000),
            "count": len(results),
        },
        "summary": {
            "by_status_group": by_group,
        },
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe RT REST2 endpoints from generated OpenAPI")
    parser.add_argument("--workspace", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--base-url", default="http://localhost")
    parser.add_argument("--max-ops", type=int, default=120)
    parser.add_argument("--timeout", type=int, default=8)
    parser.add_argument("--auth-mode", choices=["auto", "none", "token", "basic", "cookie"], default="auto")
    parser.add_argument("--token", default="")
    parser.add_argument("--user", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--cookie", default="")
    args = parser.parse_args()

    report = probe(args)
    workspace = Path(args.workspace).resolve()
    out_path = workspace / "out" / "probe-results.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "probe_results": str(out_path.relative_to(workspace)),
                "count": report["meta"]["count"],
                "by_status_group": report["summary"]["by_status_group"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
