"""Write the FastAPI OpenAPI schema to ``frontend/openapi.json`` (step 3.10).

The UI's TypeScript types are generated from this file, so a route or field that
changes in Python cannot silently drift away from the client:

    python -m scripts.export_openapi
    cd frontend && npm run gen:api
"""

from __future__ import annotations

import json
from pathlib import Path

from app.main import app

OUT = Path(__file__).resolve().parents[2] / "frontend" / "openapi.json"


def main() -> int:
    OUT.write_text(json.dumps(app.openapi(), indent=2) + "\n", encoding="utf-8")
    schemas = len(app.openapi().get("components", {}).get("schemas", {}))
    print(f"wrote {OUT} ({schemas} schemas)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
