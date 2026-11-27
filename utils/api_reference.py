"""Generate docs/ENDPOINTS.md, the per-endpoint reference, from the API's own OpenAPI schema.

    python -m utils.api_reference            # rewrite docs/ENDPOINTS.md
    python -m utils.api_reference --check    # exit 1 when docs/ENDPOINTS.md is out of date

Every endpoint gets: what it does (its docstring), its path and query parameters, its body (each
field: type, required or default, description), its response (the model, or a JSON object) and a
``curl`` line. The text comes from api.py only, so it cannot drift: tests/test_api_reference.py fails
when the file is not regenerated after an endpoint changes.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "docs" / "ENDPOINTS.md"
BASE = "http://localhost:8005"
# The order areas appear in: the way a client meets them (operations, world, train, play, ...).
TAG_ORDER = ["Operations", "Configuration", "World authoring", "Persistent variables", "Character stats",
             "Engine logic", "Author tools", "Evolution and play", "Jobs", "Saved games", "Evaluation",
             "Engine export", "Records", "Studio", "Artifacts"]
METHOD_ORDER = {"get": 0, "post": 1, "put": 2, "patch": 3, "delete": 4}


def _schema():
    sys.path.insert(0, str(ROOT))
    import api

    return api.api.openapi()


def _ref(spec, schema):
    while isinstance(schema, dict) and "$ref" in schema:
        schema = spec["components"]["schemas"][schema["$ref"].rsplit("/", 1)[1]]
    return schema or {}


def _type(spec, schema):
    """A short type: str, int, bool, list[str], ModelName, A | B."""
    if not isinstance(schema, dict):
        return "any"
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[1]
    if "enum" in schema:
        return " | ".join(f"`{v}`" for v in schema["enum"])
    if "const" in schema:
        return f"`{schema['const']}`"
    for key in ("anyOf", "oneOf"):
        if key in schema:
            parts = [_type(spec, s) for s in schema[key] if s.get("type") != "null"]
            nullable = any(s.get("type") == "null" for s in schema[key])
            return " | ".join(dict.fromkeys(parts)) + (" | null" if nullable else "")
    kind = schema.get("type")
    if kind == "array":
        return f"list[{_type(spec, schema.get('items', {}))}]"
    if kind == "object":
        extra = schema.get("additionalProperties")
        return f"dict[str, {_type(spec, extra)}]" if isinstance(extra, dict) and extra else "object"
    return {"string": "str", "integer": "int", "number": "number", "boolean": "bool"}.get(kind, kind or "any")


def _one_line(text):
    return " ".join(_md(text).split())


def _md(text):
    """Docstring text as Markdown: reStructuredText ``code`` becomes `code`."""
    return (text or "").replace("``", "`")


def _cell(text):
    """A table cell: a `|` inside it (a union type, a choice) must not split the row."""
    return str(text).replace("|", "\\|")


def _default(value):
    return json.dumps(value, ensure_ascii=False)


def _fields(spec, schema):
    """Rows (name, type, required/default, description) of an object schema's top-level fields."""
    schema = _ref(spec, schema)
    required = set(schema.get("required") or ())
    rows = []
    for name, field in (schema.get("properties") or {}).items():
        need = "required" if name in required else (
            f"default {_default(field['default'])}" if "default" in field else "optional")
        rows.append((name, _type(spec, field), need, _one_line(field.get("description"))))
    return rows


def _example_body(spec, schema):
    """A minimal JSON body: the required fields with a placeholder of their type."""
    schema = _ref(spec, schema)
    body = {}
    for name in schema.get("required") or ():
        field = _ref(spec, (schema.get("properties") or {}).get(name, {}))
        kind = field.get("type")
        body[name] = {"integer": 1, "number": 1.0, "boolean": True, "array": [], "object": {}}.get(kind, "...")
    return body


def _curl(method, path, params, body_kind, example):
    path_filled = path
    for param in params:
        if param["in"] == "path":
            path_filled = path_filled.replace("{" + param["name"] + "}", f"<{param['name']}>")
    parts = ["curl"]
    if method != "get":
        parts.append(f"-X {method.upper()}")
    if body_kind == "json":
        parts += ["-H 'Content-Type: application/json'", f"-d '{json.dumps(example, ensure_ascii=False)}'"]
    elif body_kind == "form":
        parts += [f"-F {name}=@<{name}>" for name in example]
    parts.append(f"'{BASE}{path_filled}'")
    return " ".join(parts)


def _anchor(method, path):
    """The GitHub anchor of an endpoint's heading (``#### `GET /v1/x/{id}` ``)."""
    text = f"{method.upper()} {path}".lower()
    return "".join(c for c in text.replace(" ", "-") if c.isalnum() or c in "-_")


def anchor(method, path):
    return _anchor(method, path)


def _endpoint(spec, path, method, op):
    out = [f"#### `{method.upper()} {path}`", ""]
    description = (op.get("description") or op.get("summary") or "").strip()
    if description:
        out += [_md(description), ""]
    params = [p for p in op.get("parameters") or () if p["in"] in ("path", "query")]
    if params:
        out += ["| Parameter | In | Type | Required / default | Description |", "|---|---|---|---|---|"]
        for p in params:
            schema = p.get("schema") or {}
            need = "required" if p.get("required") else (
                f"default {_default(schema['default'])}" if "default" in schema else "optional")
            out.append(f"| `{p['name']}` | {p['in']} | {_cell(_type(spec, schema))} | {_cell(need)} | "
                       f"{_cell(_one_line(p.get('description') or schema.get('description')))} |")
        out.append("")
    body_kind, example = None, {}
    content = ((op.get("requestBody") or {}).get("content") or {})
    if "application/json" in content:
        body_kind = "json"
        schema = content["application/json"].get("schema") or {}
        name = _type(spec, schema)
        example = _example_body(spec, schema)
        required = (op.get("requestBody") or {}).get("required")
        out.append(f"**Body** (JSON, {name}{'' if required else ', optional'}):")
        rows = _fields(spec, schema)
        if rows:
            out += ["", "| Field | Type | Required / default | Description |", "|---|---|---|---|"]
            out += [f"| `{n}` | {_cell(t)} | {_cell(r)} | {_cell(d)} |" for n, t, r, d in rows]
        out.append("")
    elif "multipart/form-data" in content:
        body_kind = "form"
        schema = _ref(spec, content["multipart/form-data"].get("schema") or {})
        required = set(schema.get("required") or ())
        names = list((schema.get("properties") or {}))
        example = [n for n in names if n in required] or names[:1]
        out.append("**Body** (multipart form): " + ", ".join(
            f"`{n}`" + (" (required)" if n in required else " (optional)") for n in names) + ".")
        out.append("")
    ok = next((code for code in op.get("responses") or {} if code.startswith("2")), "200")
    response = ((op["responses"].get(ok) or {}).get("content") or {})
    if "application/json" in response:
        model = _type(spec, response["application/json"].get("schema") or {})
        out.append(f"**Response** `{ok}`: " + (f"{model}." if model not in ("any", "object") else "a JSON object (above)."))
    else:
        out.append(f"**Response** `{ok}`: a file or other content.")
    errors = sorted(code for code in op.get("responses") or {} if code[0] in "45" and code != "422")
    if errors:
        out[-1] += " Also " + ", ".join(f"`{c}`" for c in errors) + "."
    out += ["", "```bash", _curl(method, path, params, body_kind, example), "```", ""]
    return out


def render():
    spec = _schema()
    by_tag = {}
    for path, item in spec["paths"].items():
        for method, op in item.items():
            if method in METHOD_ORDER:
                by_tag.setdefault((op.get("tags") or ["Other"])[0], []).append((path, method, op))
    tags = [t for t in TAG_ORDER if t in by_tag] + sorted(t for t in by_tag if t not in TAG_ORDER)
    count = sum(len(v) for v in by_tag.values())
    out = ["# Endpoint reference", "",
           "<!-- Generated by `python -m utils.api_reference` from api.py: do not edit by hand. -->", "",
           f"Every endpoint of the API ({count}), by area: what it does, its parameters, its body, its response "
           "and a `curl` line (base URL "
           f"`{BASE}`; add `-H 'X-ANE-User: <id>'` where a run is started). How the API behaves (status codes, "
           "writes, limits, sessions) and worked flows are in [API.md](API.md); the studio's views and the "
           "endpoints each one calls are in [FRONTEND.md](FRONTEND.md). Errors answer `{\"detail\": ...}`; "
           "every endpoint with a body or parameters can also answer `422` (invalid shape). The live, "
           "interactive version of this list is `/docs` on a running API.", "", "## Areas", ""]
    for tag in tags:
        out.append(f"- [{tag}](#{tag.lower().replace(' ', '-')}) ({len(by_tag[tag])})")
    out.append("")
    for tag in tags:
        out += [f"## {tag}", ""]
        for path, method, op in sorted(by_tag[tag], key=lambda e: (e[0], METHOD_ORDER[e[1]])):
            out += _endpoint(spec, path, method, op)
    return "\n".join(out).rstrip() + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail when docs/ENDPOINTS.md is out of date")
    args = parser.parse_args(argv)
    text = render()
    if args.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != text:
            print("docs/ENDPOINTS.md is out of date: run `python -m utils.api_reference`.")
            return 1
        print("docs/ENDPOINTS.md is up to date.")
        return 0
    TARGET.write_text(text, encoding="utf-8")
    print(f"Wrote {TARGET.relative_to(ROOT)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
