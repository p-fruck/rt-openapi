#!/usr/bin/env python3
import argparse
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

HTTP_ORDER = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
HTTP_SET = set(HTTP_ORDER)
TEST_HTTP_CALL = "get|head|delete|post|post_json|put|put_json"


@dataclass(frozen=True)
class RouteDef:
    resource_class: str
    source_file: str
    regex_pattern: str
    methods: Tuple[str, ...]
    methods_explicit: bool
    roles: Tuple[str, ...]


def find_matching_brace(text: str, start_idx: int) -> int:
    depth = 0
    i = start_idx
    while i < len(text):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def get_sub_block(text: str, sub_name: str) -> Optional[str]:
    m = re.search(rf"\bsub\s+{re.escape(sub_name)}\b", text)
    if not m:
        return None
    open_brace = text.find("{", m.end())
    if open_brace < 0:
        return None
    close_brace = find_matching_brace(text, open_brace)
    if close_brace < 0:
        return None
    return text[open_brace + 1 : close_brace]


def parse_methods(text: str, extends: Sequence[str], roles: Sequence[str]) -> Tuple[List[str], bool]:
    methods: List[str] = []
    explicit = False
    block = get_sub_block(text, "allowed_methods")
    if block:
        seen = set()
        for method in re.findall(r"'([A-Z]+)'", block):
            if method in HTTP_SET and method not in seen:
                seen.add(method)
                methods.append(method)
        explicit = True

    if not methods:
        base = {"GET", "HEAD"}
        if any("ProcessPOSTasGET" in role or "QueryByJSON" in role for role in roles):
            base.add("POST")
        if any("Record::Writable" in role for role in roles):
            base.update({"PUT", "POST"})
        if any("RequestBodyIsJSON" in role for role in roles):
            base.add("PUT")
        if any("Writable" in ext for ext in extends):
            base.add("PUT")
            base.add("POST")
        if any("Record::Deletable" in role for role in roles):
            base.add("DELETE")
        if any("Deletable" in ext for ext in extends):
            base.add("DELETE")
        methods = [m for m in HTTP_ORDER if m in base]

    return sorted(set(methods), key=HTTP_ORDER.index), explicit


def parse_with_roles(text: str) -> List[str]:
    roles: List[str] = []
    for match in re.finditer(r"\bwith\b(.*?);", text, flags=re.S):
        chunk = match.group(1)
        for role in re.findall(r"'([^']+)'", chunk):
            if role.startswith("RT::"):
                roles.append(role)
    return sorted(set(roles))


def parse_extends(text: str) -> List[str]:
    extends: List[str] = []
    for match in re.finditer(r"\bextends\b(.*?);", text, flags=re.S):
        chunk = match.group(1)
        for parent in re.findall(r"'([^']+)'", chunk):
            extends.append(parent)
    return sorted(set(extends))


def parse_resource_file(path: Path) -> List[RouteDef]:
    text = path.read_text(encoding="utf-8", errors="replace")
    pkg = re.search(r"^\s*package\s+([A-Za-z0-9_:]+)\s*;", text, flags=re.M)
    if not pkg:
        return []

    resource_class = pkg.group(1)
    extends = parse_extends(text)
    roles = parse_with_roles(text)
    methods, methods_explicit = parse_methods(text, extends, roles)
    methods = tuple(methods)

    route_patterns = re.findall(r"regex\s*=>\s*qr\{([^}]*)\}", text)
    routes: List[RouteDef] = []
    for pattern in route_patterns:
        routes.append(
            RouteDef(
                resource_class=resource_class,
                source_file=str(path),
                regex_pattern=pattern.strip(),
                methods=methods,
                methods_explicit=methods_explicit,
                roles=tuple(roles),
            )
        )
    return routes


def infer_route_specific_methods(route: RouteDef, path: str) -> List[str]:
    methods = set(route.methods)
    has_param = "{" in path
    has_writable = any("Record::Writable" in role for role in route.roles)
    has_deletable = any("Record::Deletable" in role for role in route.roles)

    if route.methods_explicit:
        return sorted(methods, key=HTTP_ORDER.index)

    if has_writable:
        if has_param:
            methods.add("PUT")
            methods.discard("POST")
        else:
            methods.add("POST")
            methods.discard("PUT")

    if has_deletable and not has_param:
        methods.discard("DELETE")

    return sorted(methods, key=HTTP_ORDER.index)


def is_plain_alternative(content: str) -> bool:
    parts = content.split("|")
    if len(parts) <= 1:
        return False
    for part in parts:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", part):
            return False
    return True


def expand_literal_optional_chars(literal: str) -> List[str]:
    out = [""]
    i = 0
    while i < len(literal):
        ch = literal[i]
        if ch == "\\" and i + 1 < len(literal):
            token = literal[i + 1]
            for idx in range(len(out)):
                out[idx] += token
            i += 2
            continue

        if i + 1 < len(literal) and literal[i + 1] == "?" and re.fullmatch(r"[A-Za-z0-9_-]", ch):
            with_char = [value + ch for value in out]
            out = out + with_char
            i += 2
            continue

        for idx in range(len(out)):
            out[idx] += ch
        i += 1
    return out


def split_top_level_groups(pattern: str) -> List[Tuple[str, str, str]]:
    parts: List[Tuple[str, str, str]] = []
    buf: List[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern):
            buf.append(pattern[i : i + 2])
            i += 2
            continue

        if ch == "(":
            literal_before = "".join(buf)
            buf = []
            depth = 1
            j = i + 1
            while j < len(pattern) and depth > 0:
                if pattern[j] == "\\" and j + 1 < len(pattern):
                    j += 2
                    continue
                if pattern[j] == "(":
                    depth += 1
                elif pattern[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            content = pattern[i + 1 : j]
            quantifier = ""
            if j + 1 < len(pattern) and pattern[j + 1] in "?+*":
                quantifier = pattern[j + 1]
                i = j + 2
            else:
                i = j + 1
            parts.append(("literal", literal_before, ""))
            parts.append(("group", content, quantifier))
            continue

        buf.append(ch)
        i += 1

    parts.append(("literal", "".join(buf), ""))
    return parts


def normalize_pattern(pattern: str) -> str:
    p = pattern.strip()
    if p.startswith("^"):
        p = p[1:]
    if p.endswith("$"):
        p = p[:-1]
    p = re.sub(r"/\?+$", "", p)
    if p == "/rt?":
        return "/(rt|r)"
    return p


def group_to_tokens(content: str, param_idx: int) -> List[str]:
    c = content
    if c.startswith("?:"):
        c = c[2:]

    if is_plain_alternative(c):
        return c.split("|")

    if c in (r"\d+", r"\d*", r"\d{1,}"):
        return [f"{{id{param_idx}}}"]
    if c in (r"[^/]+", r".+"):
        return [f"{{name{param_idx}}}"]

    return [f"{{param{param_idx}}}"]


def expand_route_pattern(pattern: str) -> List[str]:
    if pattern == r"^/users(?:/(privileged|unprivileged)?)?$":
        return ["/users", "/users/privileged", "/users/unprivileged"]

    p = normalize_pattern(pattern)
    parts = split_top_level_groups(p)

    expansions = [""]
    param_counter = 0

    for part_type, value, quantifier in parts:
        if part_type == "literal":
            lit_options = expand_literal_optional_chars(value)
            next_expansions: List[str] = []
            for base in expansions:
                for lit in lit_options:
                    next_expansions.append(base + lit)
            expansions = next_expansions
            continue

        param_counter += 1
        tokens = group_to_tokens(value, param_counter)
        if quantifier == "?":
            tokens = [""] + tokens
        next_expansions = []
        for base in expansions:
            for token in tokens:
                next_expansions.append(base + token)
        expansions = next_expansions

    normalized = set()
    for candidate in expansions:
        c = candidate.replace(r"\/", "/")
        c = c.replace("//", "/")
        if not c.startswith("/"):
            c = "/" + c
        c = re.sub(r"/+", "/", c)
        c = re.sub(r"/+$", "", c)
        if c == "":
            c = "/"
        normalized.add(c)

    return sorted(normalized)


def collect_route_defs(resource_dir: Path) -> List[RouteDef]:
    route_defs: List[RouteDef] = []
    for file in sorted(resource_dir.glob("*.pm")):
        name = file.name
        if name.endswith("_Overlay.pm") or name.endswith("_Vendor.pm") or name.endswith("_Local.pm"):
            continue
        route_defs.extend(parse_resource_file(file))
    return route_defs


def perl_method_to_http(name: str) -> Optional[str]:
    map_ = {
        "get": "GET",
        "head": "HEAD",
        "delete": "DELETE",
        "post": "POST",
        "post_json": "POST",
        "put": "PUT",
        "put_json": "PUT",
    }
    return map_.get(name.lower())


def collect_test_method_hints(test_dir: Path) -> Dict[str, List[str]]:
    hints: Dict[str, set] = {}
    if not test_dir.exists():
        return {}

    pattern = re.compile(
        r"\$mech->(get|head|delete|post|post_json|put|put_json)\s*\(\s*(['\"])(/REST/2\.0/[^'\"]+)\2",
        flags=re.I,
    )

    for file in sorted(test_dir.glob("*.t")):
        text = file.read_text(encoding="utf-8", errors="replace")
        for method_name, _, path in pattern.findall(text):
            http = perl_method_to_http(method_name)
            if not http:
                continue
            hints.setdefault(path, set()).add(http)

    return {k: sorted(v, key=HTTP_ORDER.index) for k, v in hints.items()}


def split_top_level_args(expr: str) -> List[str]:
    parts: List[str] = []
    cur: List[str] = []
    depth = 0
    in_single = False
    in_double = False
    i = 0
    while i < len(expr):
        ch = expr[i]
        if ch == "\\" and i + 1 < len(expr):
            cur.append(expr[i : i + 2])
            i += 2
            continue

        if not in_double and ch == "'":
            in_single = not in_single
            cur.append(ch)
            i += 1
            continue

        if not in_single and ch == '"':
            in_double = not in_double
            cur.append(ch)
            i += 1
            continue

        if not in_single and not in_double:
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth = max(0, depth - 1)
            elif ch == "," and depth == 0:
                part = "".join(cur).strip()
                if part:
                    parts.append(part)
                cur = []
                i += 1
                continue

        cur.append(ch)
        i += 1

    tail = "".join(cur).strip()
    if tail:
        parts.append(tail)
    return parts


def extract_string_literals(expr: str) -> List[str]:
    literals: List[str] = []
    for quote in ("'", '"'):
        pattern = re.compile(rf"{re.escape(quote)}((?:\\.|[^{re.escape(quote)}])*){re.escape(quote)}")
        for m in pattern.finditer(expr):
            literals.append(m.group(1))
    return literals


def normalize_test_path(path_expr: str) -> Optional[str]:
    expr = " ".join(path_expr.strip().split())
    if "url_for_hypermedia" in expr:
        return None

    literals = extract_string_literals(expr)
    if not literals and "$rest_base_path" not in expr:
        return None

    combined = "".join(literals)
    combined = combined.replace("$rest_base_path", "/REST/2.0")
    if "$rest_base_path" in expr and "/REST/2.0" not in combined:
        combined = "/REST/2.0" + combined

    combined = re.sub(r"/REST/2\.0(?:/REST/2\.0)+", "/REST/2.0", combined)
    rest_pos = combined.find("/REST/2.0")
    if rest_pos >= 0:
        combined = combined[rest_pos:]

    combined = combined.lstrip(",")
    combined = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", r"{\1}", combined)
    combined = re.sub(r"\$([A-Za-z_][A-Za-z0-9_]*)", r"{\1}", combined)
    combined = combined.replace("\\/", "/")

    if "/REST/2.0" not in combined:
        return None

    path = combined.split("/REST/2.0", 1)[1]
    if not path:
        path = "/"
    if not path.startswith("/"):
        path = "/" + path
    path = path.split("?", 1)[0]
    path = re.sub(r"/+", "/", path)
    path = re.sub(r"/+$", "", path) or "/"
    return path


def collect_test_evidence(test_dir: Path) -> Dict[str, object]:
    if not test_dir.exists():
        return {"operations": []}

    operation_map: Dict[Tuple[str, str], Dict[str, object]] = {}
    call_start = re.compile(rf"(?:my\s+)?\$(\w+)\s*=\s*\$mech->({TEST_HTTP_CALL})\s*\(")
    status_line = re.compile(r"\bis\s*\(\s*\$(\w+)->code\s*,\s*(\d{3})\s*\)")

    for file in sorted(test_dir.glob("*.t")):
        lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
        pending: Dict[str, Dict[str, object]] = {}
        i = 0
        while i < len(lines):
            line = lines[i]
            m = call_start.search(line)
            if m:
                var_name = m.group(1)
                method_name = m.group(2)

                call_lines = [line[m.start() :]]
                paren_balance = call_lines[0].count("(") - call_lines[0].count(")")
                j = i + 1
                while j < len(lines) and (paren_balance > 0 or ";" not in call_lines[-1]):
                    call_lines.append(lines[j])
                    paren_balance += lines[j].count("(") - lines[j].count(")")
                    if lines[j].strip().endswith(";") and paren_balance <= 0:
                        break
                    j += 1

                call_text = "\n".join(call_lines)
                open_idx = call_text.find("(")
                close_idx = call_text.rfind(")")
                args_text = call_text[open_idx + 1 : close_idx] if open_idx >= 0 and close_idx > open_idx else ""
                args = split_top_level_args(args_text)
                path_expr = args[0] if args else ""
                path = normalize_test_path(path_expr)

                http = perl_method_to_http(method_name)
                if path and http:
                    key = (path, http)
                    entry = operation_map.setdefault(
                        key,
                        {
                            "path": path,
                            "method": http,
                            "files": set(),
                            "status_codes": set(),
                            "request_body": "json" if method_name.endswith("_json") else "unknown",
                            "calls": 0,
                        },
                    )
                    entry["files"].add(str(file))
                    entry["calls"] += 1
                    if method_name.endswith("_json"):
                        entry["request_body"] = "json"

                    pending[var_name] = {"key": key, "line": i + 1}

                i = max(i + 1, j)

            status = status_line.search(line)
            if status:
                var_name = status.group(1)
                code = status.group(2)
                req = pending.get(var_name)
                if req and abs((i + 1) - int(req["line"])) <= 50:
                    key = req["key"]
                    operation_map[key]["status_codes"].add(code)

            i += 1

    operations = []
    for key in sorted(operation_map):
        op = operation_map[key]
        operations.append(
            {
                "path": op["path"],
                "method": op["method"],
                "calls": op["calls"],
                "request_body": op["request_body"],
                "status_codes": sorted(op["status_codes"]),
                "files": sorted(op["files"]),
            }
        )
    return {"operations": operations}


def template_to_regex(path_template: str) -> re.Pattern:
    escaped = re.escape(path_template)
    escaped = re.sub(r"\\\{[^}]+\\\}", r"[^/]+", escaped)
    return re.compile(rf"^{escaped}$")


def merge_method_hints(paths: Dict[str, Dict], hints: Dict[str, List[str]]) -> None:
    compiled = [(template_to_regex(p), p) for p in paths]
    for hinted_path, methods in hints.items():
        if not hinted_path.startswith("/REST/2.0"):
            continue
        rel_path = hinted_path[len("/REST/2.0") :]
        if not rel_path:
            rel_path = "/"
        rel_path = rel_path.rstrip("/") or "/"
        for regex, path in compiled:
            if regex.match(rel_path):
                current = set(paths[path]["methods"])
                for m in methods:
                    if m in HTTP_SET:
                        current.add(m)
                paths[path]["methods"] = sorted(current, key=HTTP_ORDER.index)


def build_inventory(route_defs: List[RouteDef], test_hints: Dict[str, List[str]]) -> Dict:
    routes = []
    paths: Dict[str, Dict] = {}

    for route in route_defs:
        expanded = expand_route_pattern(route.regex_pattern)
        for rel_path in expanded:
            path_entry = paths.setdefault(
                rel_path,
                {
                    "path": rel_path,
                    "methods": [],
                    "resources": set(),
                    "regex_patterns": set(),
                    "roles": set(),
                    "source_files": set(),
                },
            )
            route_methods = infer_route_specific_methods(route, rel_path)
            for method in route_methods:
                if method not in path_entry["methods"]:
                    path_entry["methods"].append(method)
            path_entry["resources"].add(route.resource_class)
            path_entry["regex_patterns"].add(route.regex_pattern)
            path_entry["source_files"].add(route.source_file)
            for role in route.roles:
                path_entry["roles"].add(role)

            routes.append(
                {
                    "resource_class": route.resource_class,
                    "regex_pattern": route.regex_pattern,
                    "path": rel_path,
                    "methods": route_methods,
                    "methods_explicit": route.methods_explicit,
                    "roles": list(route.roles),
                    "source_file": route.source_file,
                }
            )

    merge_method_hints(paths, test_hints)

    normalized_paths = []
    for path in sorted(paths):
        entry = paths[path]
        normalized_paths.append(
            {
                "path": path,
                "methods": sorted(set(entry["methods"]), key=HTTP_ORDER.index),
                "resources": sorted(entry["resources"]),
                "regex_patterns": sorted(entry["regex_patterns"]),
                "roles": sorted(entry["roles"]),
                "source_files": sorted(entry["source_files"]),
            }
        )

    return {
        "meta": {
            "rest_path": "/REST/2.0",
            "extraction": "static-route-regex + allowed_methods + test-method-hints",
            "resource_count": len({r.resource_class for r in route_defs}),
            "route_regex_count": len(route_defs),
            "path_count": len(normalized_paths),
        },
        "paths": normalized_paths,
        "routes": sorted(routes, key=lambda x: (x["path"], x["resource_class"], x["regex_pattern"])),
    }


def op_id(method: str, path: str) -> str:
    segments = [s for s in path.split("/") if s]
    words = []
    for seg in segments:
        if seg.startswith("{") and seg.endswith("}"):
            words.append("by_" + seg[1:-1])
        else:
            words.append(re.sub(r"[^A-Za-z0-9]+", "_", seg))
    suffix = "_".join(words) if words else "root"
    return f"{method.lower()}_{suffix}".lower()


def tag_for_path(path: str) -> str:
    first = next((seg for seg in path.split("/") if seg and not seg.startswith("{")), "misc")
    return first


def path_parameters(path: str) -> List[Dict[str, object]]:
    params = []
    for name in re.findall(r"\{([^}]+)\}", path):
        params.append(
            {
                "name": name,
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
            }
        )
    return params


def response_for_method(method: str) -> Tuple[str, Dict[str, object]]:
    if method == "DELETE":
        return "204", {"description": "No Content"}
    if method == "POST":
        return "200", {"description": "OK"}
    return "200", {"description": "OK"}


def status_description(code: str) -> str:
    if code == "200":
        return "OK"
    if code == "201":
        return "Created"
    if code == "202":
        return "Accepted"
    if code == "204":
        return "No Content"
    if code == "400":
        return "Bad Request"
    if code == "401":
        return "Unauthorized"
    if code == "403":
        return "Forbidden"
    if code == "404":
        return "Not Found"
    if code == "409":
        return "Conflict"
    if code == "422":
        return "Unprocessable Content"
    if code == "500":
        return "Internal Server Error"
    return "Response"


def equivalent_path_templates(path_a: str, path_b: str) -> bool:
    def to_shape(path: str) -> str:
        return re.sub(r"\{[^}]+\}", "{}", path)

    return to_shape(path_a) == to_shape(path_b)


def index_test_evidence(test_evidence: Dict[str, object]) -> Dict[Tuple[str, str], Dict[str, object]]:
    index: Dict[Tuple[str, str], Dict[str, object]] = {}
    ops = test_evidence.get("operations", []) if isinstance(test_evidence, dict) else []
    for op in ops:
        if not isinstance(op, dict):
            continue
        path = op.get("path")
        method = op.get("method")
        if isinstance(path, str) and isinstance(method, str) and method in HTTP_SET:
            index[(path, method)] = op
    return index


def find_evidence_for_operation(
    evidence_index: Dict[Tuple[str, str], Dict[str, object]],
    op_path: str,
    method: str,
) -> Optional[Dict[str, object]]:
    direct = evidence_index.get((op_path, method))
    if direct:
        return direct
    for (path, meth), op in evidence_index.items():
        if meth == method and equivalent_path_templates(path, op_path):
            return op
    return None


def load_probe_analysis(path: Path) -> Dict[str, object]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def index_runtime_probe_overrides(
    probe_analysis: Dict[str, object],
) -> Tuple[Dict[str, List[str]], List[str], Dict[str, object]]:
    status_by_operation: Dict[str, set] = {}
    head_405_ops: set = set()

    undocumented = probe_analysis.get("undocumented_status", []) if isinstance(probe_analysis, dict) else []
    if isinstance(undocumented, list):
        for item in undocumented:
            if not isinstance(item, dict):
                continue
            op_id = item.get("operation_id")
            status = item.get("status")
            if not isinstance(op_id, str):
                continue
            status_str = str(status)
            if re.fullmatch(r"\d{3}", status_str):
                status_by_operation.setdefault(op_id, set()).add(status_str)

    head_405 = probe_analysis.get("head_method_not_allowed", []) if isinstance(probe_analysis, dict) else []
    if isinstance(head_405, list):
        for item in head_405:
            if not isinstance(item, dict):
                continue
            op_id = item.get("operation_id")
            if isinstance(op_id, str):
                head_405_ops.add(op_id)

    meta = {
        "source": "out/probe-analysis.json",
        "runtime_status_operation_count": len(status_by_operation),
        "head_405_operation_count": len(head_405_ops),
    }

    return (
        {op: sorted(codes, key=int) for op, codes in status_by_operation.items()},
        sorted(head_405_ops),
        meta,
    )


def build_openapi(
    inventory: Dict,
    rt_commit: str,
    test_evidence: Dict[str, object],
    runtime_status_by_operation: Dict[str, List[str]],
    head_405_operations: List[str],
    runtime_meta: Dict[str, object],
) -> Dict:
    paths_obj: Dict[str, Dict[str, object]] = {}
    evidence_index = index_test_evidence(test_evidence)
    head_405_set = set(head_405_operations)

    for entry in inventory["paths"]:
        path = entry["path"]
        methods = entry["methods"]
        path_item: Dict[str, object] = {}

        params = path_parameters(path)
        if params:
            path_item["parameters"] = params

        for method in methods:
            operation_id = op_id(method, path)
            if method == "HEAD" and operation_id in head_405_set:
                # Deterministic runtime pruning: operation consistently returned 405 in probes.
                continue

            status_code, status_response = response_for_method(method)
            responses: Dict[str, object] = {
                status_code: status_response,
                "default": {"$ref": "#/components/responses/Error"},
            }

            evidence = find_evidence_for_operation(evidence_index, path, method)
            if evidence:
                observed = evidence.get("status_codes", [])
                if isinstance(observed, list):
                    for code in observed:
                        if isinstance(code, str) and re.fullmatch(r"\d{3}", code):
                            if code not in responses:
                                responses[code] = {"description": status_description(code)}

            runtime_codes = runtime_status_by_operation.get(operation_id, [])
            for code in runtime_codes:
                if code not in responses:
                    responses[code] = {
                        "description": status_description(code),
                        "x-rt-runtime-observed": True,
                    }

            operation_obj: Dict[str, object] = {
                "operationId": operation_id,
                "tags": [tag_for_path(path)],
                "description": (
                    "Auto-generated RT REST2 operation skeleton. "
                    "Schema and examples to be enriched in later phases."
                ),
                "responses": responses,
                "security": [
                    {"tokenAuth": []},
                    {"basicAuth": []},
                    {"cookieAuth": []},
                ],
                "x-rt-resource-classes": entry["resources"],
                "x-rt-route-regex": entry["regex_patterns"],
            }

            if method in ("POST", "PUT", "PATCH"):
                operation_obj["requestBody"] = {
                    "required": False,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "description": "Generic JSON payload placeholder; refine in later phases.",
                                "additionalProperties": True,
                            }
                        }
                    },
                }

            if evidence:
                if evidence.get("request_body") == "json" and method in ("POST", "PUT", "PATCH"):
                    operation_obj["requestBody"] = {
                        "required": False,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "description": "Observed in RT tests as JSON request body.",
                                    "oneOf": [
                                        {"type": "object", "additionalProperties": True},
                                        {"type": "array", "items": {"type": "object", "additionalProperties": True}},
                                    ],
                                }
                            }
                        },
                    }

                operation_obj["x-rt-test-evidence"] = {
                    "calls": evidence.get("calls", 0),
                    "status_codes": evidence.get("status_codes", []),
                    "files": evidence.get("files", []),
                }

            if runtime_codes:
                operation_obj["x-rt-runtime-evidence"] = {
                    "status_codes": runtime_codes,
                    "source": runtime_meta.get("source", "out/probe-analysis.json"),
                }

            path_item[method.lower()] = operation_obj

        if len(path_item) == 1 and "parameters" in path_item:
            # Every operation on this path was pruned.
            continue

        paths_obj[path] = path_item

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "RT REST 2.0 API",
            "version": f"0.1.0-rt-{rt_commit[:12]}",
            "description": (
                "Phase-3 deterministic skeleton generated from RT source route regexes, "
                "RT test evidence, and runtime probe-driven response/method refinements."
            ),
        },
        "servers": [{"url": "http://localhost", "description": "Local RT instance"}],
        "paths": paths_obj,
        "x-rt-runtime-overrides": runtime_meta,
        "components": {
            "securitySchemes": {
                "tokenAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "RT token",
                    "description": "Send token in Authorization header."
                },
                "basicAuth": {"type": "http", "scheme": "basic"},
                "cookieAuth": {"type": "apiKey", "in": "cookie", "name": "RT_SID"},
            },
            "responses": {
                "Error": {
                    "description": "Error response",
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "message": {"type": "string"},
                                    "status": {"type": "integer"},
                                },
                                "additionalProperties": True,
                            }
                        }
                    },
                }
            },
        },
    }


def scalar_to_yaml(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        if value == "" or re.search(r"[:#\n\-\{\}\[\],&\*\?\|<>=%@!`\"]", value) or value.strip() != value:
            return json.dumps(value)
        return value
    return json.dumps(value)


def dump_yaml(obj: object, indent: int = 0) -> str:
    sp = " " * indent
    if isinstance(obj, dict):
        lines: List[str] = []
        for key, value in obj.items():
            if isinstance(value, (dict, list)):
                if isinstance(value, list) and not value:
                    lines.append(f"{sp}{key}: []")
                elif isinstance(value, dict) and not value:
                    lines.append(f"{sp}{key}: {{}}")
                else:
                    lines.append(f"{sp}{key}:")
                    lines.append(dump_yaml(value, indent + 2))
            else:
                lines.append(f"{sp}{key}: {scalar_to_yaml(value)}")
        return "\n".join(lines)

    if isinstance(obj, list):
        if not obj:
            return f"{sp}[]"
        lines = []
        for item in obj:
            if isinstance(item, (dict, list)):
                lines.append(f"{sp}-")
                lines.append(dump_yaml(item, indent + 2))
            else:
                lines.append(f"{sp}- {scalar_to_yaml(item)}")
        return "\n".join(lines)

    return f"{sp}{scalar_to_yaml(obj)}"


def read_rt_commit(rt_dir: Path) -> str:
    head = rt_dir / ".git" / "HEAD"
    if head.exists():
        text = head.read_text(encoding="utf-8", errors="replace").strip()
        if text.startswith("ref:"):
            ref = text.split(":", 1)[1].strip()
            ref_path = rt_dir / ".git" / ref
            if ref_path.exists():
                return ref_path.read_text(encoding="utf-8", errors="replace").strip()
        if re.fullmatch(r"[0-9a-f]{40}", text):
            return text
    return os.environ.get("RT_COMMIT", "unknown")


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract deterministic RT REST2 endpoint inventory and OpenAPI skeleton")
    parser.add_argument("--workspace", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    rt_dir = workspace / "vendor" / "rt"
    resource_dir = rt_dir / "lib" / "RT" / "REST2" / "Resource"
    test_dir = rt_dir / "t" / "rest2"

    if not resource_dir.exists():
        raise SystemExit(f"Missing resource directory: {resource_dir}")

    route_defs = collect_route_defs(resource_dir)
    test_hints = collect_test_method_hints(test_dir)
    inventory = build_inventory(route_defs, test_hints)
    test_evidence = collect_test_evidence(test_dir)
    probe_analysis = load_probe_analysis(workspace / "out" / "probe-analysis.json")
    runtime_status_by_operation, head_405_operations, runtime_meta = index_runtime_probe_overrides(probe_analysis)

    rt_commit = read_rt_commit(rt_dir)
    inventory["meta"]["rt_commit"] = rt_commit

    out_dir = workspace / "out"
    spec_dir = workspace / "spec"
    out_dir.mkdir(parents=True, exist_ok=True)
    spec_dir.mkdir(parents=True, exist_ok=True)

    inventory_path = out_dir / "endpoint-inventory.json"
    test_evidence_path = out_dir / "test-evidence.json"
    runtime_overrides_path = out_dir / "runtime-overrides.json"
    inventory_path.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    test_evidence_path.write_text(json.dumps(test_evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    runtime_overrides_path.write_text(
        json.dumps(
            {
                "runtime_status_by_operation": runtime_status_by_operation,
                "head_405_operations": head_405_operations,
                "meta": runtime_meta,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    openapi = build_openapi(
        inventory,
        rt_commit,
        test_evidence,
        runtime_status_by_operation,
        head_405_operations,
        runtime_meta,
    )
    spec_json_path = spec_dir / "openapi.json"
    spec_yaml_path = spec_dir / "openapi.yaml"
    spec_json_path.write_text(json.dumps(openapi, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    spec_yaml_path.write_text(dump_yaml(openapi) + "\n", encoding="utf-8")

    summary = {
        "rt_commit": rt_commit,
        "resources": inventory["meta"]["resource_count"],
        "route_regexes": inventory["meta"]["route_regex_count"],
        "paths": inventory["meta"]["path_count"],
        "inventory": str(inventory_path.relative_to(workspace)),
        "test_evidence": str(test_evidence_path.relative_to(workspace)),
        "runtime_overrides": str(runtime_overrides_path.relative_to(workspace)),
        "openapi_json": str(spec_json_path.relative_to(workspace)),
        "openapi_yaml": str(spec_yaml_path.relative_to(workspace)),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
