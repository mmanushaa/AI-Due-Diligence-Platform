"""Generate TypeScript types for the UI from the FastAPI OpenAPI schema (step 3.10).

``openapi-typescript`` needs the TypeScript 5 compiler API and this project builds with
TypeScript 7, so this script does the small part of that job the UI actually needs:
every ``components.schemas`` entry becomes an exported TypeScript type. The client
imports those names, so a schema change in Python is a compile error in the UI.

    python -m scripts.export_openapi
    python -m scripts.generate_ts_types
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "frontend" / "openapi.json"
OUT = ROOT / "frontend" / "src" / "lib" / "api-types.ts"

HEADER = """// Generated from backend/app (FastAPI OpenAPI schema) by backend/scripts/generate_ts_types.py.
// Do not edit by hand: run `python -m scripts.export_openapi` from backend/ and then
// `python -m scripts.generate_ts_types` from backend/ after changing a Pydantic model.

"""


def ts_type(schema: dict[str, Any], indent: int = 0) -> str:
    if not schema:
        return "unknown"
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]
    if "anyOf" in schema:
        parts = [ts_type(part, indent) for part in schema["anyOf"]]
        return " | ".join(dict.fromkeys(parts))
    if "oneOf" in schema:
        return " | ".join(dict.fromkeys(ts_type(part, indent) for part in schema["oneOf"]))
    if "const" in schema:
        return json.dumps(schema["const"])
    if "enum" in schema:
        return " | ".join(json.dumps(value) for value in schema["enum"])
    kind = schema.get("type")
    if kind == "array":
        item = ts_type(schema.get("items", {}), indent)
        return f"({item})[]" if "|" in item else f"{item}[]"
    if kind == "object" or "properties" in schema:
        properties: dict[str, Any] = schema.get("properties") or {}
        if not properties:
            extra = schema.get("additionalProperties")
            return f"Record<string, {ts_type(extra, indent)}>" if isinstance(extra, dict) else "Record<string, unknown>"
        required = set(schema.get("required") or [])
        pad = " " * (indent + 2)
        lines = ["{"]
        for name, value in properties.items():
            optional = "" if name in required else "?"
            lines.append(f"{pad}{name}{optional}: {ts_type(value, indent + 2)};")
        lines.append(" " * indent + "}")
        return "\n".join(lines)
    if kind == "integer" or kind == "number":
        return "number"
    if kind == "boolean":
        return "boolean"
    if kind == "null":
        return "null"
    if kind == "string":
        return "string"
    return "unknown"


def main() -> int:
    document = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    schemas = document.get("components", {}).get("schemas", {})
    chunks = [HEADER]
    for name, schema in schemas.items():
        body = ts_type(schema)
        if body.startswith("{"):
            chunks.append(f"export interface {name} {body}\n\n")
        else:
            chunks.append(f"export type {name} = {body};\n\n")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("".join(chunks), encoding="utf-8")
    print(f"wrote {OUT} ({len(schemas)} types)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
