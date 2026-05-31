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


def build_openapi(inventory: Dict, rt_commit: str) -> Dict:
    paths_obj: Dict[str, Dict[str, object]] = {}

    for entry in inventory["paths"]:
        path = entry["path"]
        methods = entry["methods"]
        path_item: Dict[str, object] = {}

        params = path_parameters(path)
        if params:
            path_item["parameters"] = params

        for method in methods:
            status_code, status_response = response_for_method(method)
            path_item[method.lower()] = {
                "operationId": op_id(method, path),
                "tags": [tag_for_path(path)],
                "description": (
                    "Auto-generated RT REST2 operation skeleton. "
                    "Schema and examples to be enriched in later phases."
                ),
                "responses": {
                    status_code: status_response,
                    "default": {"$ref": "#/components/responses/Error"},
                },
                "security": [
                    {"tokenAuth": []},
                    {"basicAuth": []},
                    {"cookieAuth": []},
                ],
                "x-rt-resource-classes": entry["resources"],
                "x-rt-route-regex": entry["regex_patterns"],
            }

        paths_obj[path] = path_item

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "RT REST 2.0 API",
            "version": f"0.1.0-rt-{rt_commit[:12]}",
            "description": (
                "Phase-1 deterministic skeleton generated from RT source route regexes "
                "and allowed_methods declarations."
            ),
        },
        "servers": [{"url": "http://localhost", "description": "Local RT instance"}],
        "paths": paths_obj,
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

    rt_commit = read_rt_commit(rt_dir)
    inventory["meta"]["rt_commit"] = rt_commit

    out_dir = workspace / "out"
    spec_dir = workspace / "spec"
    out_dir.mkdir(parents=True, exist_ok=True)
    spec_dir.mkdir(parents=True, exist_ok=True)

    inventory_path = out_dir / "endpoint-inventory.json"
    inventory_path.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    openapi = build_openapi(inventory, rt_commit)
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
        "openapi_json": str(spec_json_path.relative_to(workspace)),
        "openapi_yaml": str(spec_yaml_path.relative_to(workspace)),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
