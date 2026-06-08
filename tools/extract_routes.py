#!/usr/bin/env python3
import argparse
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

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


KNOWN_QUERY_PARAMS = {
    "page",
    "per_page",
    "order",
    "orderby",
    "find_disabled_rows",
    "fields",
    "query",
    "simple",
    "category",
    "group",
    "user",
    "type",
}


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


def collect_resource_query_params(resource_dir: Path) -> Dict[str, List[str]]:
    mapping: Dict[str, Set[str]] = {}
    for file in sorted(resource_dir.glob("*.pm")):
        name = file.name
        if name.endswith("_Overlay.pm") or name.endswith("_Vendor.pm") or name.endswith("_Local.pm"):
            continue

        text = file.read_text(encoding="utf-8", errors="replace")
        pkg = re.search(r"^\s*package\s+([A-Za-z0-9_:]+)\s*;", text, flags=re.M)
        if not pkg:
            continue
        resource_class = pkg.group(1)

        params = set(re.findall(r"request->param\('([A-Za-z0-9_\[\]]+)'\)", text))
        params = {p for p in params if p in KNOWN_QUERY_PARAMS}
        if params:
            mapping[resource_class] = params

    return {k: sorted(v) for k, v in sorted(mapping.items())}


def collect_resource_content_types(resource_dir: Path) -> Dict[str, Dict[str, List[str]]]:
    mapping: Dict[str, Dict[str, Set[str]]] = {}
    for file in sorted(resource_dir.glob("*.pm")):
        name = file.name
        if name.endswith("_Overlay.pm") or name.endswith("_Vendor.pm") or name.endswith("_Local.pm"):
            continue

        text = file.read_text(encoding="utf-8", errors="replace")
        pkg = re.search(r"^\s*package\s+([A-Za-z0-9_:]+)\s*;", text, flags=re.M)
        if not pkg:
            continue
        resource_class = pkg.group(1)

        accepted = set()
        provided = set()

        accepted_block = get_sub_block(text, "content_types_accepted")
        provided_block = get_sub_block(text, "content_types_provided")

        if accepted_block:
            accepted.update(re.findall(r"'([A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+)'", accepted_block))
        if provided_block:
            provided.update(re.findall(r"'([A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+)'", provided_block))

        if accepted or provided:
            mapping[resource_class] = {
                "accepted": accepted,
                "provided": provided,
            }

    out: Dict[str, Dict[str, List[str]]] = {}
    for resource_class in sorted(mapping):
        out[resource_class] = {
            "accepted": sorted(mapping[resource_class]["accepted"]),
            "provided": sorted(mapping[resource_class]["provided"]),
        }
    return out


def perl_type_to_openapi_schema(type_name: str, is_numeric: bool, nullable: bool) -> Dict[str, object]:
    t = type_name.lower().strip()

    schema: Dict[str, object]
    if "datetime" in t or "timestamp" in t:
        schema = {"type": "string", "format": "date-time"}
    elif t == "date":
        schema = {"type": "string", "format": "date"}
    elif any(x in t for x in ("int", "smallint", "bigint")) or is_numeric:
        schema = {"type": "integer"}
    elif any(x in t for x in ("decimal", "float", "double", "numeric", "real")):
        schema = {"type": "number"}
    elif "bool" in t:
        schema = {"type": "boolean"}
    else:
        schema = {"type": "string"}

    if nullable:
        value_type = schema.get("type")
        if isinstance(value_type, str):
            schema["type"] = [value_type, "null"]
    return schema


def extract_core_accessible_fields(text: str) -> Dict[str, Dict[str, object]]:
    block = get_sub_block(text, "_CoreAccessible")
    if not block:
        return {}

    start = block.find("{")
    if start < 0:
        return {}
    end = find_matching_brace(block, start)
    if end < 0:
        return {}

    inner = block[start + 1 : end]
    field_re = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*=>\s*\{([^{}]*)\}", re.S)
    fields: Dict[str, Dict[str, object]] = {}
    for name, meta_text in field_re.findall(inner):
        if not re.search(r"\bread\s*=>\s*1\b", meta_text):
            continue

        type_match = re.search(r"\btype\s*=>\s*'([^']+)'", meta_text)
        default_match = re.search(r"\bdefault\s*=>\s*(undef|'[^']*')", meta_text)
        is_numeric_match = re.search(r"\bis_numeric\s*=>\s*(\d+)", meta_text)

        type_name = type_match.group(1) if type_match else "varchar"
        default_raw = default_match.group(1) if default_match else "''"
        nullable = default_raw == "undef"
        is_numeric = bool(is_numeric_match and is_numeric_match.group(1) == "1")

        fields[name] = {
            "type": type_name,
            "nullable": nullable,
            "is_numeric": is_numeric,
            "schema": perl_type_to_openapi_schema(type_name, is_numeric, nullable),
        }

    return fields


def collect_rt_record_meta(rt_lib_dir: Path) -> Dict[str, Dict[str, Dict[str, object]]]:
    meta: Dict[str, Dict[str, Dict[str, object]]] = {}
    for file in sorted(rt_lib_dir.glob("*.pm")):
        text = file.read_text(encoding="utf-8", errors="replace")
        pkg_match = re.search(r"^\s*package\s+([A-Za-z0-9_:]+)\s*;", text, flags=re.M)
        if not pkg_match:
            continue
        package = pkg_match.group(1)
        fields = extract_core_accessible_fields(text)
        if fields:
            meta[package] = fields
    return meta


def collect_resource_record_classes(
    resource_dir: Path,
    rt_record_meta: Dict[str, Dict[str, Dict[str, object]]],
) -> Dict[str, str]:
    mapping: Dict[str, str] = {}

    def candidate_variants(class_name: str) -> List[str]:
        variants = [class_name]
        if class_name.endswith("s"):
            variants.append(class_name[:-1])
        return variants

    for file in sorted(resource_dir.glob("*.pm")):
        text = file.read_text(encoding="utf-8", errors="replace")
        pkg_match = re.search(r"^\s*package\s+([A-Za-z0-9_:]+)\s*;", text, flags=re.M)
        if not pkg_match:
            continue

        resource_class = pkg_match.group(1)
        short = resource_class.split("::")[-1]
        candidates: List[str] = []

        for found in re.findall(r"\brecord_class\s*=>\s*'([^']+)'", text):
            candidates.append(found)
        for found in re.findall(r"\bcollection_class\s*=>\s*'([^']+)'", text):
            candidates.extend(f"RT::{name}" for name in candidate_variants(found.replace("RT::", "")))

        for name in candidate_variants(short):
            candidates.append(f"RT::{name}")

        seen: Set[str] = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            if candidate in rt_record_meta:
                mapping[resource_class] = candidate
                break

    return mapping


def record_component_name(record_class: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", record_class).strip("_") + "_Record"


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


def extract_perl_var_assignments(lines: List[str]) -> Dict[str, str]:
    assignments: Dict[str, str] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.search(r"\bmy\s+\$(\w+)\s*=\s*([\[{])", line)
        if not m:
            i += 1
            continue

        var_name = m.group(1)
        start_char = m.group(2)
        open_char = start_char
        close_char = "}" if open_char == "{" else "]"

        start_idx = line.find(open_char, m.end() - 1)
        chunk_lines = [line[start_idx:]] if start_idx >= 0 else []
        depth = 1 if start_idx >= 0 else 0
        j = i + 1
        while j < len(lines) and depth > 0:
            chunk_lines.append(lines[j])
            depth += lines[j].count(open_char) - lines[j].count(close_char)
            if depth <= 0 and ";" in lines[j]:
                break
            j += 1

        if chunk_lines:
            text = "\n".join(chunk_lines)
            semi = text.rfind(";")
            if semi >= 0:
                text = text[:semi]
            assignments[var_name] = text.strip()

        i = max(i + 1, j)
    return assignments


def perl_literal_to_json_value(literal: str) -> Optional[object]:
    text = literal.strip()
    if not text:
        return None

    # Replace Perl variables with stable placeholder strings.
    text = re.sub(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?", '"<var>"', text)
    text = text.replace("=>", ":")
    text = re.sub(r"\bundef\b", "null", text)

    # Quote bare keys in hash literals.
    text = re.sub(r"([\{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:", r'\1"\2":', text)
    # Remove trailing commas before closing.
    text = re.sub(r",\s*([}\]])", r"\1", text)
    # Normalize single-quoted strings.
    text = re.sub(r"'([^'\\]*(?:\\.[^'\\]*)*)'", lambda m: json.dumps(m.group(1)), text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def collect_test_examples(test_dir: Path) -> Dict[str, object]:
    if not test_dir.exists():
        return {"operations": []}

    operation_examples: Dict[Tuple[str, str], Dict[str, object]] = {}
    call_start = re.compile(rf"(?:my\s+)?\$(\w+)\s*=\s*\$mech->({TEST_HTTP_CALL})\s*\(")

    for file in sorted(test_dir.glob("*.t")):
        lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
        var_assignments = extract_perl_var_assignments(lines)
        pending_last_op: Optional[Tuple[str, str]] = None

        i = 0
        while i < len(lines):
            line = lines[i]
            m = call_start.search(line)
            if m:
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
                    item = operation_examples.setdefault(
                        key,
                        {
                            "path": path,
                            "method": http,
                            "request_example": None,
                            "response_key_hints": set(),
                            "files": set(),
                        },
                    )
                    item["files"].add(str(file))
                    pending_last_op = key

                    if http in ("POST", "PUT", "PATCH") and len(args) >= 2 and item["request_example"] is None:
                        payload_expr = args[1].strip()
                        payload_literal = payload_expr
                        var_ref = re.fullmatch(r"\$(\w+)", payload_expr)
                        if var_ref:
                            payload_literal = var_assignments.get(var_ref.group(1), payload_expr)
                        parsed_payload = perl_literal_to_json_value(payload_literal)
                        if parsed_payload is not None:
                            item["request_example"] = parsed_payload

                i = max(i + 1, j)

            # Collect lightweight response key hints from json_response field assertions.
            if pending_last_op:
                m_content = re.findall(r"\$content->\{([A-Za-z0-9_]+)\}", line)
                m_inline = re.findall(r"json_response->\{([A-Za-z0-9_]+)\}", line)
                for key_name in m_content + m_inline:
                    operation_examples[pending_last_op]["response_key_hints"].add(key_name)

            i += 1

    operations = []
    for key in sorted(operation_examples):
        op = operation_examples[key]
        operations.append(
            {
                "path": op["path"],
                "method": op["method"],
                "request_example": op["request_example"],
                "response_key_hints": sorted(op["response_key_hints"]),
                "files": sorted(op["files"]),
            }
        )
    return {"operations": operations}


def index_test_examples(test_examples: Dict[str, object]) -> Dict[Tuple[str, str], Dict[str, object]]:
    index: Dict[Tuple[str, str], Dict[str, object]] = {}
    ops = test_examples.get("operations", []) if isinstance(test_examples, dict) else []
    for op in ops:
        if not isinstance(op, dict):
            continue
        path = op.get("path")
        method = op.get("method")
        if isinstance(path, str) and isinstance(method, str) and method in HTTP_SET:
            index[(path, method)] = op
    return index


def find_test_example_for_operation(
    example_index: Dict[Tuple[str, str], Dict[str, object]],
    op_path: str,
    method: str,
) -> Optional[Dict[str, object]]:
    direct = example_index.get((op_path, method))
    if direct:
        return direct
    for (path, meth), op in example_index.items():
        if meth == method and equivalent_path_templates(path, op_path):
            return op
    return None


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
        schema: Dict[str, object] = {"type": "string"}
        if name.startswith("id"):
            schema = {"type": "integer", "format": "int64"}
        params.append(
            {
                "name": name,
                "in": "path",
                "required": True,
                "schema": schema,
            }
        )
    return params


def is_collection_entry(entry: Dict[str, object]) -> bool:
    roles = entry.get("roles", [])
    if isinstance(roles, list) and any(isinstance(r, str) and "Collection::" in r for r in roles):
        return True
    path = entry.get("path", "")
    if isinstance(path, str) and path in ("/queues", "/users", "/groups", "/tickets", "/assets", "/articles"):
        return True
    return False


def has_resource(entry: Dict[str, object], resource_class: str) -> bool:
    resources = entry.get("resources", [])
    if not isinstance(resources, list):
        return False
    return any(r == resource_class for r in resources if isinstance(r, str))


def has_role(entry: Dict[str, object], role_suffix: str) -> bool:
    roles = entry.get("roles", [])
    if not isinstance(roles, list):
        return False
    return any(isinstance(r, str) and r.endswith(role_suffix) for r in roles)


def response_schema_ref_for_entry(entry: Dict[str, object], method: str) -> str:
    if method in ("GET", "HEAD", "POST") and has_resource(entry, "RT::REST2::Resource::Transactions"):
        return "#/components/schemas/TransactionCollectionResponse"
    if is_collection_entry(entry):
        return "#/components/schemas/CollectionResponse"
    return "#/components/schemas/RecordObject"


def build_operation_description(entry: Dict[str, object], path: str, method: str) -> str:
    notes: List[str] = ["Auto-generated RT REST2 operation skeleton."]

    if method == "POST" and path == "/ticket":
        notes.append("Creates a new ticket.")
        notes.append(
            "Live validation in this RT instance accepted `Queue` as numeric id (`1`) while "
            "`Queue=General` returned 403; prefer queue id when create permission errors mention queue name mismatches."
        )

    if has_role(entry, "Collection::QueryByJSON"):
        notes.append(
            "Supports JSON filter arrays via POST request bodies (ProcessPOSTasGET behavior)."
        )
    if has_role(entry, "Collection::QueryBySQL"):
        notes.append("Supports SQL-like filtering through the query parameter `query`.")

    if has_resource(entry, "RT::REST2::Resource::Transactions") and path.endswith("/history"):
        notes.append("Returns transaction history for the parent record.")
        notes.append("Use the `fields` query parameter to request additional transaction fields.")
        notes.append(
            "Requested fields can be present but empty when a transaction type does not carry a value "
            "(for example `Content` on non-comment transactions)."
        )
        if path.startswith("/ticket/") and method in ("GET", "POST"):
            notes.append(
                "Ticket comments are transactions where `Type` is `Comment`; "
                "to read comments first filter to `Type=Comment`."
            )
            notes.append(
                "In observed RT behavior, comment transaction `Content` can still be empty; "
                "retrieve the comment body from attachments via "
                "`GET /transaction/{id}` -> `GET /transaction/{id}/attachments` -> `GET /attachment/{id}` "
                "(attachment `Content` is base64)."
            )
            notes.append(
                "Example filter: "
                "(for example `POST /ticket/{id}/history` with `[{\"field\":\"Type\",\"operator\":\"=\",\"value\":\"Comment\"}]`)."
            )

    return " ".join(notes)


def default_query_filter_example(entry: Dict[str, object], path: str) -> Dict[str, object]:
    if has_resource(entry, "RT::REST2::Resource::Transactions") and path.startswith("/ticket/") and path.endswith("/history"):
        return {"field": "Type", "operator": "=", "value": "Comment"}
    return {"field": "Type", "operator": "=", "value": "Set"}


def query_parameter_refs(entry: Dict[str, object], method: str, resource_query_params: Dict[str, List[str]]) -> List[Dict[str, str]]:
    if method not in ("GET", "HEAD"):
        return []

    resources = entry.get("resources", [])
    discovered: Set[str] = set()
    if isinstance(resources, list):
        for resource in resources:
            if isinstance(resource, str):
                for p in resource_query_params.get(resource, []):
                    discovered.add(p)

    # Infer common collection controls from roles when resource files don't
    # explicitly call request->param for each supported query key.
    if has_role(entry, "Collection::QueryByJSON") or has_role(entry, "Collection::QueryBySQL"):
        discovered.update({"page", "per_page", "order", "orderby"})
    if has_role(entry, "Collection::QueryByJSON"):
        discovered.add("fields")
    if has_role(entry, "Collection::QueryBySQL"):
        discovered.update({"query", "simple"})

    refs = []
    for name in sorted(discovered):
        refs.append({"$ref": f"#/components/parameters/{name}"})
    return refs


def request_body_schema_for_operation(
    entry: Dict[str, object],
    path: str,
    method: str,
    evidence: Optional[Dict[str, object]],
) -> Dict[str, object]:
    roles = entry.get("roles", []) if isinstance(entry.get("roles", []), list) else []

    if method == "POST" and path == "/ticket":
        return {
            "$ref": "#/components/schemas/TicketCreateRequest",
        }

    if method == "POST" and path.startswith("/ticket/") and path.endswith("/comment"):
        return {
            "$ref": "#/components/schemas/TicketCommentCreateRequest",
        }

    if evidence and evidence.get("request_body") == "json":
        return {
            "description": "Observed in RT tests as JSON request body.",
            "oneOf": [
                {"type": "object", "additionalProperties": True},
                {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            ],
        }

    if any(isinstance(r, str) and "Collection::QueryByJSON" in r for r in roles):
        return {
            "description": "JSON search filter list.",
            "type": "array",
            "items": {"$ref": "#/components/schemas/SearchFilter"},
        }

    if any(isinstance(r, str) and "RequestBodyIsJSON" in r for r in roles):
        return {
            "description": "JSON payload.",
            "oneOf": [
                {"type": "object", "additionalProperties": True},
                {"type": "array", "items": {"type": "string"}},
            ],
        }

    return {
        "type": "object",
        "description": "Generic JSON payload placeholder; refine in later phases.",
        "additionalProperties": True,
    }


def media_types_for_entry(entry: Dict[str, object], resource_content_types: Dict[str, Dict[str, List[str]]]) -> Dict[str, List[str]]:
    accepted: Set[str] = set()
    provided: Set[str] = set()
    resources = entry.get("resources", [])
    if isinstance(resources, list):
        for resource in resources:
            if not isinstance(resource, str):
                continue
            ct = resource_content_types.get(resource, {})
            if isinstance(ct, dict):
                accepted.update(ct.get("accepted", []))
                provided.update(ct.get("provided", []))
    return {
        "accepted": sorted(accepted),
        "provided": sorted(provided),
    }


def schema_for_request_media_type(media_type: str, default_json_schema: Dict[str, object]) -> Dict[str, object]:
    if media_type == "application/json":
        return default_json_schema
    if media_type in ("text/plain", "text/html"):
        return {"type": "string"}
    if media_type == "multipart/form-data":
        return {"type": "object", "additionalProperties": True}
    return {"type": "string"}


def apply_ticket_live_overrides(operation_obj: Dict[str, object], path: str, method: str) -> None:
    if method == "POST" and path == "/ticket":
        request_body = operation_obj.get("requestBody")
        if isinstance(request_body, dict):
            request_body["required"] = True
            content = request_body.get("content")
            if isinstance(content, dict) and "application/json" in content:
                content["application/json"] = {
                    "schema": {"$ref": "#/components/schemas/TicketCreateRequest"},
                    "example": {
                        "Subject": "OpenAPI root create 1780243466",
                        "Queue": "1",
                        "Content": "Initial body from API",
                        "ContentType": "text/plain",
                    },
                }

        operation_obj["responses"]["201"] = {
            "description": "Created",
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/TicketCreateResponse"},
                    "example": {
                        "id": "3",
                        "_url": "http://localhost:8083/REST/2.0/ticket/3",
                        "type": "ticket",
                    },
                }
            },
        }
        operation_obj["x-rt-live-evidence"] = {
            "proven": True,
            "notes": [
                "Observed status: 201 Created.",
                "Queue accepted as numeric id '1' in this environment.",
            ],
            "source": "out/live-ticket-flow.json",
        }

    if method == "PUT" and path.startswith("/ticket/") and path.count("{") == 1:
        request_body = operation_obj.get("requestBody")
        if isinstance(request_body, dict):
            content = request_body.get("content")
            if isinstance(content, dict) and "application/json" in content:
                content["application/json"]["example"] = {
                    "Subject": "OpenAPI root updated 1780243466",
                }

        responses = operation_obj.get("responses", {})
        if isinstance(responses, dict) and "200" in responses:
            resp_200 = responses.get("200")
            if isinstance(resp_200, dict):
                resp_200["content"] = {
                    "application/json": {
                        "schema": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "example": [
                            "Ticket 3: Subject changed from 'OpenAPI root create 1780243466' to 'OpenAPI root updated 1780243466'"
                        ],
                    }
                }

    if method == "POST" and path.startswith("/ticket/") and path.endswith("/comment"):
        request_body = operation_obj.get("requestBody")
        if isinstance(request_body, dict):
            request_body["required"] = True
            content = request_body.get("content")
            if isinstance(content, dict) and "application/json" in content:
                content["application/json"] = {
                    "schema": {"$ref": "#/components/schemas/TicketCommentCreateRequest"},
                    "example": {
                        "Content": "OpenAPI verified comment text",
                        "ContentType": "text/plain",
                    },
                }

        operation_obj["responses"]["201"] = {
            "description": "Created",
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/TicketCommentCreateResponse"},
                    "example": ["Comments added"],
                }
            },
        }
        operation_obj["x-rt-live-evidence"] = {
            "proven": True,
            "notes": [
                "POST /ticket/1/comment with application/json requires ContentType in this RT instance.",
                "Observed status: 201 Created.",
            ],
            "source": "out/live-ticket-flow.json",
        }

    if method == "GET" and path.startswith("/ticket/") and path.endswith("/history"):
        response_200 = operation_obj.get("responses", {}).get("200")
        if isinstance(response_200, dict):
            content = response_200.get("content")
            if isinstance(content, dict) and "application/json" in content:
                content["application/json"]["example"] = {
                    "count": 1,
                    "total": 1,
                    "pages": 1,
                    "per_page": 20,
                    "page": 1,
                    "items": [
                        {
                            "id": "50",
                            "type": "transaction",
                            "_url": "/REST/2.0/transaction/50",
                            "Type": "Comment",
                            "Content": "",
                            "Created": "2026-05-31T16:00:31Z",
                            "Creator": {
                                "id": "root",
                                "type": "user",
                                "_url": "/REST/2.0/user/root",
                            },
                        }
                    ],
                }

    if method == "POST" and path.startswith("/ticket/") and path.endswith("/history"):
        request_body = operation_obj.get("requestBody")
        if isinstance(request_body, dict):
            content = request_body.get("content")
            if isinstance(content, dict) and "application/json" in content:
                content["application/json"]["example"] = [
                    {"field": "Type", "operator": "=", "value": "Comment"}
                ]
        response_200 = operation_obj.get("responses", {}).get("200")
        if isinstance(response_200, dict):
            content = response_200.get("content")
            if isinstance(content, dict) and "application/json" in content:
                content["application/json"]["example"] = {
                    "count": 1,
                    "total": 1,
                    "pages": 1,
                    "per_page": 20,
                    "page": 1,
                    "items": [
                        {
                            "id": "50",
                            "type": "transaction",
                            "_url": "/REST/2.0/transaction/50",
                        }
                    ],
                }


def default_response_example(schema_ref: str) -> Dict[str, object]:
    if schema_ref == "#/components/schemas/TransactionCollectionResponse":
        return {
            "count": 2,
            "total": None,
            "pages": 1,
            "per_page": 20,
            "page": 1,
            "next_page": None,
            "prev_page": None,
            "items": [
                {
                    "id": 101,
                    "type": "transaction",
                    "_url": "/REST/2.0/transaction/101",
                    "Type": "Create",
                    "Content": "Asset created",
                    "ContentType": "text/plain",
                },
                {
                    "id": 102,
                    "type": "transaction",
                    "_url": "/REST/2.0/transaction/102",
                    "Type": "Set",
                    "Field": "Status",
                    "OldValue": "new",
                    "NewValue": "allocated",
                    "Content": "",
                    "ContentType": "",
                },
            ],
        }
    if schema_ref == "#/components/schemas/CollectionResponse":
        return {
            "count": 1,
            "total": 1,
            "pages": 1,
            "per_page": 20,
            "page": 1,
            "next_page": None,
            "prev_page": None,
            "items": [
                {
                    "id": 1,
                    "type": "record",
                    "_url": "/REST/2.0/example/1",
                    "_hyperlinks": [],
                }
            ],
        }
    return {
        "id": 1,
        "type": "record",
        "_url": "/REST/2.0/example/1",
        "_hyperlinks": [],
    }


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
    resource_query_params: Dict[str, List[str]],
    resource_content_types: Dict[str, Dict[str, List[str]]],
    test_examples: Dict[str, object],
    rt_record_meta: Dict[str, Dict[str, Dict[str, object]]],
    resource_record_classes: Dict[str, str],
) -> Dict:
    paths_obj: Dict[str, Dict[str, object]] = {}
    evidence_index = index_test_evidence(test_evidence)
    example_index = index_test_examples(test_examples)
    head_405_set = set(head_405_operations)

    record_components: Dict[str, str] = {
        record_class: record_component_name(record_class)
        for record_class in sorted(set(resource_record_classes.values()))
        if record_class in rt_record_meta
    }
    generated_collection_schemas: Dict[str, Dict[str, object]] = {}

    def infer_record_component_for_entry(entry: Dict[str, object]) -> Optional[str]:
        resources = entry.get("resources", [])
        if not isinstance(resources, list):
            return None
        for resource in resources:
            if not isinstance(resource, str):
                continue
            record_class = resource_record_classes.get(resource)
            if record_class and record_class in record_components:
                return record_components[record_class]
        return None

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
            media_types = media_types_for_entry(entry, resource_content_types)

            evidence = find_evidence_for_operation(evidence_index, path, method)
            test_example = find_test_example_for_operation(example_index, path, method)
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
                "description": build_operation_description(entry, path, method),
                "responses": responses,
                "security": [
                    {"tokenAuth": []},
                    {"basicAuth": []},
                    {"cookieAuth": []},
                ],
                "x-rt-resource-classes": entry["resources"],
                "x-rt-route-regex": entry["regex_patterns"],
            }

            query_refs = query_parameter_refs(entry, method, resource_query_params)
            if query_refs:
                operation_obj["parameters"] = query_refs

            if method in ("GET", "HEAD") and "200" in responses:
                response_media_types = media_types.get("provided") or ["application/json"]
                response_schema_ref = response_schema_ref_for_entry(entry, method)
                record_component = infer_record_component_for_entry(entry)

                if response_schema_ref == "#/components/schemas/RecordObject" and record_component:
                    response_schema_ref = f"#/components/schemas/{record_component}"
                elif response_schema_ref == "#/components/schemas/CollectionResponse" and record_component:
                    collection_component = f"{record_component}_Collection"
                    if collection_component not in generated_collection_schemas:
                        generated_collection_schemas[collection_component] = {
                            "type": "object",
                            "properties": {
                                "count": {"type": "integer"},
                                "total": {"type": ["integer", "null"]},
                                "pages": {"type": ["integer", "null"]},
                                "per_page": {"type": "integer"},
                                "page": {"type": "integer"},
                                "next_page": {"type": ["string", "null"]},
                                "prev_page": {"type": ["string", "null"]},
                                "items": {
                                    "type": "array",
                                    "items": {"$ref": f"#/components/schemas/{record_component}"},
                                },
                            },
                            "required": ["count", "items"],
                            "additionalProperties": True,
                        }
                    response_schema_ref = f"#/components/schemas/{collection_component}"
                response_content = {
                    media: {
                        "schema": {
                            "$ref": response_schema_ref
                        }
                    }
                    for media in response_media_types
                }
                operation_obj["responses"]["200"] = {
                    "description": "OK",
                    "content": response_content,
                }
                if test_example and test_example.get("response_key_hints"):
                    hints = test_example.get("response_key_hints")
                    if isinstance(hints, list) and hints:
                        example_obj = {key: "<example>" for key in hints[:20] if isinstance(key, str)}
                        for media in response_content:
                            operation_obj["responses"]["200"]["content"][media]["example"] = example_obj
                else:
                    fallback_example = default_response_example(response_schema_ref)
                    for media in response_content:
                        operation_obj["responses"]["200"]["content"][media]["example"] = fallback_example

            if method == "POST" and "200" in responses:
                response_schema_ref = response_schema_ref_for_entry(entry, method)
                record_component = infer_record_component_for_entry(entry)
                if response_schema_ref == "#/components/schemas/CollectionResponse" and record_component:
                    collection_component = f"{record_component}_Collection"
                    if collection_component in generated_collection_schemas:
                        response_schema_ref = f"#/components/schemas/{collection_component}"
                if response_schema_ref != "#/components/schemas/RecordObject":
                    response_media_types = media_types.get("provided") or ["application/json"]
                    response_content = {
                        media: {
                            "schema": {
                                "$ref": response_schema_ref
                            }
                        }
                        for media in response_media_types
                    }
                    operation_obj["responses"]["200"] = {
                        "description": "OK",
                        "content": response_content,
                    }
                    fallback_example = default_response_example(response_schema_ref)
                    for media in response_content:
                        operation_obj["responses"]["200"]["content"][media]["example"] = fallback_example

            if method in ("POST", "PUT", "PATCH"):
                default_json_schema = request_body_schema_for_operation(entry, path, method, evidence)
                request_media_types = media_types.get("accepted") or ["application/json"]
                request_content = {
                    media: {"schema": schema_for_request_media_type(media, default_json_schema)}
                    for media in request_media_types
                }
                if test_example and test_example.get("request_example") is not None:
                    req_example = test_example.get("request_example")
                    if "application/json" in request_content:
                        request_content["application/json"]["example"] = req_example
                elif has_role(entry, "Collection::QueryByJSON") and "application/json" in request_content:
                    request_content["application/json"]["example"] = [
                        default_query_filter_example(entry, path)
                    ]
                operation_obj["requestBody"] = {
                    "required": False,
                    "content": request_content,
                }

            apply_ticket_live_overrides(operation_obj, path, method)

            if evidence:

                operation_obj["x-rt-test-evidence"] = {
                    "calls": evidence.get("calls", 0),
                    "status_codes": evidence.get("status_codes", []),
                    "files": evidence.get("files", []),
                }

            if test_example:
                operation_obj["x-rt-test-examples"] = {
                    "files": test_example.get("files", []),
                    "response_key_hints": test_example.get("response_key_hints", []),
                }

            if runtime_codes:
                operation_obj["x-rt-runtime-evidence"] = {
                    "status_codes": runtime_codes,
                    "source": runtime_meta.get("source", "out/probe-analysis.json"),
                }

            if media_types.get("accepted") or media_types.get("provided"):
                operation_obj["x-rt-content-types"] = media_types

            path_item[method.lower()] = operation_obj

        if len(path_item) == 1 and "parameters" in path_item:
            # Every operation on this path was pruned.
            continue

        paths_obj[path] = path_item

    meta_record_schemas: Dict[str, Dict[str, object]] = {}
    for record_class, component_name in record_components.items():
        fields = rt_record_meta.get(record_class, {})
        properties: Dict[str, object] = {
            "id": {},
            "type": {"type": "string"},
            "_url": {"type": "string"},
            "_hyperlinks": {
                "type": "array",
                "items": {"$ref": "#/components/schemas/Hyperlink"},
            },
        }

        for field_name in sorted(fields):
            schema = fields[field_name].get("schema", {})
            if isinstance(schema, dict):
                properties[field_name] = schema

        meta_record_schemas[component_name] = {
            "type": "object",
            "properties": properties,
            "additionalProperties": True,
            "x-rt-record-class": record_class,
            "description": f"Derived from {record_class}::_CoreAccessible metadata.",
        }

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
        "servers": [{"url": "http://localhost/REST/2.0", "description": "Local RT instance"}],
        "paths": paths_obj,
        "x-rt-runtime-overrides": runtime_meta,
        "components": {
            "parameters": {
                "page": {
                    "name": "page",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "integer", "minimum": 1},
                    "description": "Page number.",
                },
                "per_page": {
                    "name": "per_page",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "integer", "minimum": 1},
                    "description": "Items per page.",
                },
                "order": {
                    "name": "order",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string", "enum": ["ASC", "DESC", "asc", "desc"]},
                    "description": "Sort order.",
                },
                "orderby": {
                    "name": "orderby",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string"},
                    "description": "Field(s) used for sorting.",
                },
                "find_disabled_rows": {
                    "name": "find_disabled_rows",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "boolean"},
                    "description": "Include disabled rows in results.",
                },
                "fields": {
                    "name": "fields",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string"},
                    "description": "Comma-separated list of fields to return.",
                },
                "query": {
                    "name": "query",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string"},
                    "description": "SQL-like search query.",
                },
                "simple": {
                    "name": "simple",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "boolean"},
                    "description": "Enable simple query parsing.",
                },
                "category": {
                    "name": "category",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string"},
                    "description": "Category selector.",
                },
                "group": {
                    "name": "group",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "integer", "format": "int64"},
                    "description": "Group identifier.",
                },
                "user": {
                    "name": "user",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string"},
                    "description": "User identifier or name.",
                },
                "type": {
                    "name": "type",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string"},
                    "description": "Lifecycle type.",
                },
            },
            "schemas": {
                "SearchFilter": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string"},
                        "operator": {"type": "string"},
                        "value": {},
                        "entry_aggregator": {"type": "string"},
                    },
                    "required": ["field", "value"],
                    "additionalProperties": True,
                },
                "TicketCommentCreateRequest": {
                    "type": "object",
                    "properties": {
                        "Content": {"type": "string"},
                        "ContentType": {"type": "string", "example": "text/plain"},
                    },
                    "required": ["Content", "ContentType"],
                    "additionalProperties": True,
                    "description": "Observed for application/json comment creation in RT REST 2.0.",
                },
                "TicketCreateRequest": {
                    "type": "object",
                    "properties": {
                        "Subject": {"type": "string"},
                        "Queue": {
                            "oneOf": [{"type": "string"}, {"type": "integer", "format": "int64"}],
                            "description": "Queue name or id.",
                        },
                        "Content": {"type": "string"},
                        "ContentType": {"type": "string", "example": "text/plain"},
                    },
                    "required": ["Subject", "Queue"],
                    "additionalProperties": True,
                    "description": "Observed for application/json ticket creation in RT REST 2.0.",
                },
                "TicketCreateResponse": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "_url": {"type": "string"},
                        "type": {"type": "string", "enum": ["ticket"]},
                    },
                    "required": ["id", "_url", "type"],
                    "additionalProperties": True,
                },
                "TicketCommentCreateResponse": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "RT returns a message list, for example ['Comments added'].",
                },
                "Hyperlink": {
                    "type": "object",
                    "properties": {
                        "ref": {"type": "string"},
                        "type": {"type": "string"},
                        "id": {},
                        "_url": {"type": "string"},
                    },
                    "additionalProperties": True,
                },
                "RecordObject": {
                    "type": "object",
                    "properties": {
                        "id": {},
                        "type": {"type": "string"},
                        "_url": {"type": "string"},
                        "_hyperlinks": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/Hyperlink"},
                        },
                    },
                    "additionalProperties": True,
                },
                "TransactionRecord": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer", "format": "int64"},
                        "type": {"type": "string", "enum": ["transaction"]},
                        "_url": {"type": "string"},
                        "_hyperlinks": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/TransactionHyperlink"},
                        },
                        "Type": {"type": "string"},
                        "Field": {"type": "string"},
                        "OldValue": {"type": ["string", "null"]},
                        "NewValue": {"type": ["string", "null"]},
                        "Content": {"type": "string"},
                        "ContentType": {"type": "string"},
                        "TimeTaken": {"type": ["number", "string", "null"]},
                        "Creator": {"$ref": "#/components/schemas/TransactionEntityRef"},
                        "Object": {"$ref": "#/components/schemas/TransactionEntityRef"},
                        "Created": {"type": "string"},
                    },
                    "required": ["id", "type", "_url"],
                    "additionalProperties": False,
                },
                "TransactionEntityRef": {
                    "type": "object",
                    "properties": {
                        "id": {"oneOf": [{"type": "integer"}, {"type": "string"}]},
                        "type": {"type": "string"},
                        "_url": {"type": "string"},
                    },
                    "required": ["id", "type", "_url"],
                    "additionalProperties": False,
                },
                "TransactionHyperlink": {
                    "type": "object",
                    "properties": {
                        "ref": {"type": "string"},
                        "type": {"type": "string"},
                        "id": {"oneOf": [{"type": "integer"}, {"type": "string"}]},
                        "_url": {"type": "string"},
                        "label": {"type": "string"},
                        "from": {"type": "string"},
                        "to": {"type": "string"},
                    },
                    "required": ["ref", "_url"],
                    "additionalProperties": False,
                },
                "CollectionResponse": {
                    "type": "object",
                    "properties": {
                        "count": {"type": "integer"},
                        "total": {"type": ["integer", "null"]},
                        "pages": {"type": ["integer", "null"]},
                        "per_page": {"type": "integer"},
                        "page": {"type": "integer"},
                        "next_page": {"type": ["string", "null"]},
                        "prev_page": {"type": ["string", "null"]},
                        "items": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/RecordObject"},
                        },
                    },
                    "required": ["count", "items"],
                    "additionalProperties": True,
                },
                "TransactionCollectionResponse": {
                    "type": "object",
                    "properties": {
                        "count": {"type": "integer"},
                        "total": {"type": ["integer", "null"]},
                        "pages": {"type": ["integer", "null"]},
                        "per_page": {"type": "integer"},
                        "page": {"type": "integer"},
                        "next_page": {"type": ["string", "null"]},
                        "prev_page": {"type": ["string", "null"]},
                        "items": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/TransactionRecord"},
                        },
                    },
                    "required": ["count", "items"],
                    "additionalProperties": False,
                },
                **meta_record_schemas,
                **generated_collection_schemas,
            },
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
    rt_lib_dir = rt_dir / "lib" / "RT"
    resource_dir = rt_dir / "lib" / "RT" / "REST2" / "Resource"
    test_dir = rt_dir / "t" / "rest2"

    if not resource_dir.exists():
        raise SystemExit(f"Missing resource directory: {resource_dir}")

    route_defs = collect_route_defs(resource_dir)
    resource_query_params = collect_resource_query_params(resource_dir)
    resource_content_types = collect_resource_content_types(resource_dir)
    rt_record_meta = collect_rt_record_meta(rt_lib_dir)
    resource_record_classes = collect_resource_record_classes(resource_dir, rt_record_meta)
    test_hints = collect_test_method_hints(test_dir)
    inventory = build_inventory(route_defs, test_hints)
    test_evidence = collect_test_evidence(test_dir)
    test_examples = collect_test_examples(test_dir)
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
    test_examples_path = out_dir / "test-examples.json"
    record_meta_path = out_dir / "record-meta.json"
    runtime_overrides_path = out_dir / "runtime-overrides.json"
    inventory_path.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    test_evidence_path.write_text(json.dumps(test_evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    test_examples_path.write_text(json.dumps(test_examples, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    record_meta_path.write_text(
        json.dumps(
            {
                "resource_record_classes": resource_record_classes,
                "rt_record_meta": rt_record_meta,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
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
        resource_query_params,
        resource_content_types,
        test_examples,
        rt_record_meta,
        resource_record_classes,
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
        "test_examples": str(test_examples_path.relative_to(workspace)),
        "record_meta": str(record_meta_path.relative_to(workspace)),
        "runtime_overrides": str(runtime_overrides_path.relative_to(workspace)),
        "openapi_json": str(spec_json_path.relative_to(workspace)),
        "openapi_yaml": str(spec_yaml_path.relative_to(workspace)),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
