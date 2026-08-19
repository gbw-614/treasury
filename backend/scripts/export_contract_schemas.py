from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.schemas import (
    AnalysisRequest,
    AnalysisResponse,
    LocalizationResult,
    OcrRun,
    RuleResult,
    VisionRun,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIRECTORY = REPOSITORY_ROOT / "schemas"
CONTRACTS = {
    "verification-request-v1.schema.json": AnalysisRequest,
    "vision-extraction-v1.schema.json": VisionRun,
    "ocr-geometry-v1.schema.json": OcrRun,
    "localization-v1.schema.json": LocalizationResult,
    "rule-result-v1.schema.json": RuleResult,
    "analysis-response-v1.schema.json": AnalysisResponse,
}


def render_schema(filename: str, model: Any) -> str:
    schema = model.model_json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"https://schemas.treasury.local/{filename}"
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Export versioned API contracts.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail when checked-in schemas differ from the Pydantic contracts.",
    )
    args = parser.parse_args()

    differences: list[str] = []
    for filename, model in CONTRACTS.items():
        destination = SCHEMA_DIRECTORY / filename
        rendered = render_schema(filename, model)
        if args.check:
            if not destination.exists() or destination.read_text() != rendered:
                differences.append(filename)
        else:
            destination.write_text(rendered)

    if differences:
        parser.error("out-of-date schemas: " + ", ".join(differences))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
