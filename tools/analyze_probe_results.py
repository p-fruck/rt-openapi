#!/usr/bin/env python3
import argparse
import json
import re
from pathlib import Path
from typing import Dict, List, Tuple

HTTP_METHODS = {"get", "head", "post", "put", "patch", "delete", "options", "trace"}


def load_json(path: Path) -> Dict[str, object]:
    if not path.exists():
        raise SystemExit(f"Missing required file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def build_operation_index(openapi: Dict[str, object]) -> Dict[str, Dict[str, object]]:
    index: Dict[str, Dict[str, object]] = {}
    paths = openapi.get("paths", {})
    if not isinstance(paths, dict):
        return index

    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method, op in path_item.items():
            if method not in HTTP_METHODS or not isinstance(op, dict):
                continue
            operation_id = op.get("operationId")
            if not isinstance(operation_id, str):
                operation_id = f"{method}_{path}"
            responses = op.get("responses", {})
            status_codes = []
            has_default = False
            if isinstance(responses, dict):
                for code in responses:
                    if code == "default":
                        has_default = True
                    elif re.fullmatch(r"\d{3}", str(code)):
                        status_codes.append(str(code))
            index[operation_id] = {
                "path": path,
                "method": method.upper(),
                "status_codes": sorted(set(status_codes)),
                "has_default": has_default,
            }
    return index


def analyze(openapi: Dict[str, object], probe: Dict[str, object]) -> Dict[str, object]:
    op_index = build_operation_index(openapi)
    results = probe.get("results", [])
    if not isinstance(results, list):
        results = []

    total = 0
    by_group: Dict[str, int] = {}
    undocumented_status: List[Dict[str, object]] = []
    unknown_operation: List[Dict[str, object]] = []
    head_405: List[Dict[str, object]] = []
    server_5xx: List[Dict[str, object]] = []
    transport_errors: List[Dict[str, object]] = []

    for entry in results:
        if not isinstance(entry, dict):
            continue
        total += 1
        status = int(entry.get("status", -1)) if str(entry.get("status", "")).lstrip("-").isdigit() else -1
        group = str(entry.get("status_group", "unknown"))
        by_group[group] = by_group.get(group, 0) + 1

        op_id = str(entry.get("operation_id", ""))
        op = op_index.get(op_id)
        if not op:
            unknown_operation.append(
                {
                    "operation_id": op_id,
                    "method": entry.get("method", ""),
                    "path_template": entry.get("path_template", ""),
                    "status": status,
                }
            )
            continue

        documented = set(op.get("status_codes", []))
        has_default = bool(op.get("has_default", False))

        if status < 0:
            transport_errors.append(
                {
                    "operation_id": op_id,
                    "method": op["method"],
                    "path": op["path"],
                    "error": entry.get("error", ""),
                }
            )
        else:
            status_s = str(status)
            if status_s not in documented:
                undocumented_status.append(
                    {
                        "operation_id": op_id,
                        "method": op["method"],
                        "path": op["path"],
                        "status": status,
                        "documented_statuses": sorted(documented),
                        "has_default_response": has_default,
                    }
                )

            if op["method"] == "HEAD" and status == 405:
                head_405.append(
                    {
                        "operation_id": op_id,
                        "path": op["path"],
                        "status": status,
                    }
                )

            if 500 <= status <= 599:
                server_5xx.append(
                    {
                        "operation_id": op_id,
                        "method": op["method"],
                        "path": op["path"],
                        "status": status,
                    }
                )

    return {
        "meta": {
            "probed_operations": total,
            "indexed_operations": len(op_index),
        },
        "summary": {
            "by_status_group": by_group,
            "undocumented_status_count": len(undocumented_status),
            "head_405_count": len(head_405),
            "server_5xx_count": len(server_5xx),
            "transport_error_count": len(transport_errors),
            "unknown_operation_count": len(unknown_operation),
        },
        "undocumented_status": undocumented_status,
        "head_method_not_allowed": head_405,
        "server_errors": server_5xx,
        "transport_errors": transport_errors,
        "unknown_operation_ids": unknown_operation,
    }


def render_markdown(analysis: Dict[str, object], probe: Dict[str, object]) -> str:
    meta = analysis.get("meta", {})
    summary = analysis.get("summary", {})
    probe_meta = probe.get("meta", {}) if isinstance(probe, dict) else {}

    lines: List[str] = []
    lines.append("# Probe Coverage Report")
    lines.append("")
    lines.append("## Scope")
    lines.append("")
    lines.append(f"- Base URL: `{probe_meta.get('base_url', 'unknown')}`")
    lines.append(f"- Auth mode: `{probe_meta.get('auth_mode', 'unknown')}`")
    if probe_meta.get("effective_auth_mode"):
        lines.append(f"- Effective auth mode: `{probe_meta.get('effective_auth_mode')}`")
    if "auth_header_present" in probe_meta:
        lines.append(f"- Auth header present: `{probe_meta.get('auth_header_present')}`")
    lines.append(f"- Probed operations: `{meta.get('probed_operations', 0)}`")
    lines.append(f"- Indexed operations in OpenAPI: `{meta.get('indexed_operations', 0)}`")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    by_group = summary.get("by_status_group", {})
    if isinstance(by_group, dict):
        for group in sorted(by_group):
            lines.append(f"- `{group}`: `{by_group[group]}`")
    lines.append(f"- Undocumented status observations: `{summary.get('undocumented_status_count', 0)}`")
    lines.append(f"- HEAD returned 405: `{summary.get('head_405_count', 0)}`")
    lines.append(f"- Server errors (5xx): `{summary.get('server_5xx_count', 0)}`")
    lines.append(f"- Transport errors: `{summary.get('transport_error_count', 0)}`")
    lines.append("")

    undocumented = analysis.get("undocumented_status", [])
    lines.append("## Top Undocumented Statuses")
    lines.append("")
    if isinstance(undocumented, list) and undocumented:
        for item in undocumented[:20]:
            lines.append(
                f"- `{item.get('method')} {item.get('path')}` observed `{item.get('status')}`; documented `{','.join(item.get('documented_statuses', [])) or 'none'}`"
            )
    else:
        lines.append("- None")
    lines.append("")

    head_405 = analysis.get("head_method_not_allowed", [])
    lines.append("## HEAD 405 Candidates")
    lines.append("")
    if isinstance(head_405, list) and head_405:
        for item in head_405[:20]:
            lines.append(f"- `{item.get('path')}` ({item.get('operation_id')})")
    else:
        lines.append("- None")
    lines.append("")

    server_5xx = analysis.get("server_errors", [])
    lines.append("## 5xx Endpoints")
    lines.append("")
    if isinstance(server_5xx, list) and server_5xx:
        for item in server_5xx[:20]:
            lines.append(f"- `{item.get('method')} {item.get('path')}` -> `{item.get('status')}`")
    else:
        lines.append("- None")
    lines.append("")

    lines.append("## Notes")
    lines.append("")
    lines.append("- Undocumented statuses include expected runtime statuses not explicitly listed in operation responses.")
    lines.append("- A `default` response may still handle these at runtime, but explicit codes improve generated clients and docs.")
    return "\n".join(lines) + "\n"


def to_undoc_set(analysis: Dict[str, object]) -> set:
    out = set()
    items = analysis.get("undocumented_status", [])
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        op = str(item.get("operation_id", ""))
        status = str(item.get("status", ""))
        if op and status:
            out.add((op, status))
    return out


def to_head405_set(analysis: Dict[str, object]) -> set:
    out = set()
    items = analysis.get("head_method_not_allowed", [])
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        op = str(item.get("operation_id", ""))
        if op:
            out.add(op)
    return out


def to_server5xx_set(analysis: Dict[str, object]) -> set:
    out = set()
    items = analysis.get("server_errors", [])
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        op = str(item.get("operation_id", ""))
        status = str(item.get("status", ""))
        if op and status:
            out.add((op, status))
    return out


def build_delta(current: Dict[str, object], previous: Dict[str, object]) -> Dict[str, object]:
    cur_undoc = to_undoc_set(current)
    prev_undoc = to_undoc_set(previous)
    cur_head405 = to_head405_set(current)
    prev_head405 = to_head405_set(previous)
    cur_5xx = to_server5xx_set(current)
    prev_5xx = to_server5xx_set(previous)

    return {
        "summary": {
            "undocumented_status_added": len(cur_undoc - prev_undoc),
            "undocumented_status_removed": len(prev_undoc - cur_undoc),
            "head_405_added": len(cur_head405 - prev_head405),
            "head_405_removed": len(prev_head405 - cur_head405),
            "server_5xx_added": len(cur_5xx - prev_5xx),
            "server_5xx_removed": len(prev_5xx - cur_5xx),
        },
        "undocumented_status": {
            "added": [
                {"operation_id": op, "status": status}
                for op, status in sorted(cur_undoc - prev_undoc)
            ],
            "removed": [
                {"operation_id": op, "status": status}
                for op, status in sorted(prev_undoc - cur_undoc)
            ],
        },
        "head_method_not_allowed": {
            "added": sorted(cur_head405 - prev_head405),
            "removed": sorted(prev_head405 - cur_head405),
        },
        "server_errors": {
            "added": [
                {"operation_id": op, "status": status}
                for op, status in sorted(cur_5xx - prev_5xx)
            ],
            "removed": [
                {"operation_id": op, "status": status}
                for op, status in sorted(prev_5xx - cur_5xx)
            ],
        },
    }


def render_delta_markdown(delta: Dict[str, object]) -> str:
    summary = delta.get("summary", {}) if isinstance(delta, dict) else {}
    lines: List[str] = []
    lines.append("# Probe Delta Report")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Undocumented status added: `{summary.get('undocumented_status_added', 0)}`")
    lines.append(f"- Undocumented status removed: `{summary.get('undocumented_status_removed', 0)}`")
    lines.append(f"- HEAD 405 added: `{summary.get('head_405_added', 0)}`")
    lines.append(f"- HEAD 405 removed: `{summary.get('head_405_removed', 0)}`")
    lines.append(f"- Server 5xx added: `{summary.get('server_5xx_added', 0)}`")
    lines.append(f"- Server 5xx removed: `{summary.get('server_5xx_removed', 0)}`")
    lines.append("")

    undoc = delta.get("undocumented_status", {}) if isinstance(delta, dict) else {}
    added_undoc = undoc.get("added", []) if isinstance(undoc, dict) else []
    removed_undoc = undoc.get("removed", []) if isinstance(undoc, dict) else []

    lines.append("## Undocumented Status Added")
    lines.append("")
    if added_undoc:
        for item in added_undoc[:40]:
            lines.append(f"- `{item.get('operation_id')}` -> `{item.get('status')}`")
    else:
        lines.append("- None")
    lines.append("")

    lines.append("## Undocumented Status Removed")
    lines.append("")
    if removed_undoc:
        for item in removed_undoc[:40]:
            lines.append(f"- `{item.get('operation_id')}` -> `{item.get('status')}`")
    else:
        lines.append("- None")
    lines.append("")

    head = delta.get("head_method_not_allowed", {}) if isinstance(delta, dict) else {}
    added_head = head.get("added", []) if isinstance(head, dict) else []
    removed_head = head.get("removed", []) if isinstance(head, dict) else []

    lines.append("## HEAD 405 Added")
    lines.append("")
    if added_head:
        for op in added_head[:40]:
            lines.append(f"- `{op}`")
    else:
        lines.append("- None")
    lines.append("")

    lines.append("## HEAD 405 Removed")
    lines.append("")
    if removed_head:
        for op in removed_head[:40]:
            lines.append(f"- `{op}`")
    else:
        lines.append("- None")
    lines.append("")

    sx = delta.get("server_errors", {}) if isinstance(delta, dict) else {}
    added_sx = sx.get("added", []) if isinstance(sx, dict) else []
    removed_sx = sx.get("removed", []) if isinstance(sx, dict) else []

    lines.append("## Server 5xx Added")
    lines.append("")
    if added_sx:
        for item in added_sx[:40]:
            lines.append(f"- `{item.get('operation_id')}` -> `{item.get('status')}`")
    else:
        lines.append("- None")
    lines.append("")

    lines.append("## Server 5xx Removed")
    lines.append("")
    if removed_sx:
        for item in removed_sx[:40]:
            lines.append(f"- `{item.get('operation_id')}` -> `{item.get('status')}`")
    else:
        lines.append("- None")

    return "\n".join(lines) + "\n"


def write_snapshot(current: Dict[str, object], workspace: Path) -> Path:
    baseline_path = workspace / "out" / "probe-analysis.baseline.json"
    baseline_path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return baseline_path


def write_delta(current: Dict[str, object], workspace: Path) -> Tuple[Path, Path]:
    baseline_path = workspace / "out" / "probe-analysis.baseline.json"
    if not baseline_path.exists():
        raise SystemExit(f"Missing baseline file: {baseline_path}")

    previous = load_json(baseline_path)
    delta = build_delta(current, previous)

    delta_json_path = workspace / "out" / "probe-delta.json"
    delta_md_path = workspace / "out" / "probe-delta.md"
    delta_json_path.write_text(json.dumps(delta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    delta_md_path.write_text(render_delta_markdown(delta), encoding="utf-8")
    return delta_json_path, delta_md_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze RT probe results against generated OpenAPI")
    parser.add_argument("--workspace", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--snapshot", action="store_true", help="Write current analysis as baseline snapshot")
    parser.add_argument("--delta", action="store_true", help="Diff current analysis against baseline snapshot")
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    openapi = load_json(workspace / "spec" / "openapi.json")
    probe = load_json(workspace / "out" / "probe-results.json")

    analysis = analyze(openapi, probe)

    analysis_path = workspace / "out" / "probe-analysis.json"
    report_path = workspace / "out" / "coverage-report.md"
    analysis_path.write_text(json.dumps(analysis, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report_path.write_text(render_markdown(analysis, probe), encoding="utf-8")

    baseline_rel = None
    delta_json_rel = None
    delta_md_rel = None
    if args.snapshot:
        baseline_path = write_snapshot(analysis, workspace)
        baseline_rel = str(baseline_path.relative_to(workspace))

    if args.delta:
        delta_json_path, delta_md_path = write_delta(analysis, workspace)
        delta_json_rel = str(delta_json_path.relative_to(workspace))
        delta_md_rel = str(delta_md_path.relative_to(workspace))

    print(
        json.dumps(
            {
                "probe_analysis": str(analysis_path.relative_to(workspace)),
                "coverage_report": str(report_path.relative_to(workspace)),
                "baseline_snapshot": baseline_rel,
                "probe_delta_json": delta_json_rel,
                "probe_delta_md": delta_md_rel,
                "summary": analysis.get("summary", {}),
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
