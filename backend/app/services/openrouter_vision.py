from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.schemas import ValidatedPanel, VisionPanelExtraction, VisionRun
from app.services.blind_request import (
    BLIND_EXTRACTION_PROMPT,
    build_blind_vision_request,
)

PROMPT_VERSION = "blind-extraction-v7"
DEFAULT_MODEL = "google/gemini-3.5-flash"
DEFAULT_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_TIMEOUT_SECONDS = 12.0


def _normalized_box_to_pixels(
    raw_box: dict[str, object] | None,
    panel: ValidatedPanel,
) -> dict[str, int] | None:
    if raw_box is None:
        return None
    try:
        x_min = int(raw_box["xMin"])
        y_min = int(raw_box["yMin"])
        x_max = int(raw_box["xMax"])
        y_max = int(raw_box["yMax"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            "Provider returned an invalid normalized evidence box"
        ) from exc
    if not (0 <= x_min < x_max <= 1000 and 0 <= y_min < y_max <= 1000):
        raise ValueError("Provider returned an out-of-range normalized evidence box")

    x = min(panel.width - 1, round(x_min * panel.width / 1000))
    y = min(panel.height - 1, round(y_min * panel.height / 1000))
    right = max(x + 1, min(panel.width, round(x_max * panel.width / 1000)))
    bottom = max(y + 1, min(panel.height, round(y_max * panel.height / 1000)))
    return {"x": x, "y": y, "width": right - x, "height": bottom - y}


def _response_schema(panel_id: str) -> dict[str, Any]:
    field_keys = [
        "brand_name",
        "class_type",
        "alcohol_content",
        "proof",
        "government_warning_heading",
        "government_warning_body",
    ]
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "panelId": {"type": "string", "const": panel_id},
            "fullText": {"type": "string"},
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "fieldKey": {"type": "string", "enum": field_keys},
                        "rawText": {"type": "string"},
                        "normalizedValue": {
                            "anyOf": [
                                {"type": "string"},
                                {"type": "number"},
                                {"type": "null"},
                            ]
                        },
                        "evidenceQuote": {"type": "string"},
                        "evidenceBox1000": {
                            "anyOf": [
                                {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {
                                        "xMin": {
                                            "type": "integer",
                                            "minimum": 0,
                                            "maximum": 999,
                                        },
                                        "yMin": {
                                            "type": "integer",
                                            "minimum": 0,
                                            "maximum": 999,
                                        },
                                        "xMax": {
                                            "type": "integer",
                                            "minimum": 1,
                                            "maximum": 1000,
                                        },
                                        "yMax": {
                                            "type": "integer",
                                            "minimum": 1,
                                            "maximum": 1000,
                                        },
                                    },
                                    "required": ["xMin", "yMin", "xMax", "yMax"],
                                },
                                {"type": "null"},
                            ]
                        },
                        "panelId": {"type": "string", "const": panel_id},
                        "legibility": {
                            "type": "string",
                            "enum": ["clear", "uncertain", "unreadable"],
                        },
                        "uncertainty": {
                            "anyOf": [{"type": "string"}, {"type": "null"}]
                        },
                    },
                    "required": [
                        "fieldKey",
                        "rawText",
                        "normalizedValue",
                        "evidenceQuote",
                        "evidenceBox1000",
                        "panelId",
                        "legibility",
                        "uncertainty",
                    ],
                },
            },
            "warningPresentation": {
                "anyOf": [
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "headingAllCaps": {
                                "anyOf": [{"type": "boolean"}, {"type": "null"}]
                            },
                            "headingOnlyBold": {
                                "anyOf": [{"type": "boolean"}, {"type": "null"}]
                            },
                            "continuousParagraph": {
                                "anyOf": [{"type": "boolean"}, {"type": "null"}]
                            },
                            "separateAndApart": {
                                "anyOf": [{"type": "boolean"}, {"type": "null"}]
                            },
                            "legibleContrast": {
                                "anyOf": [{"type": "boolean"}, {"type": "null"}]
                            },
                            "textAppearsUnusuallySmall": {
                                "anyOf": [{"type": "boolean"}, {"type": "null"}]
                            },
                            "uncertainty": {
                                "anyOf": [{"type": "string"}, {"type": "null"}]
                            },
                        },
                        "required": [
                            "headingAllCaps",
                            "headingOnlyBold",
                            "continuousParagraph",
                            "separateAndApart",
                            "legibleContrast",
                            "textAppearsUnusuallySmall",
                            "uncertainty",
                        ],
                    },
                    {"type": "null"},
                ]
            },
            "observations": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "panelId",
            "fullText",
            "fields",
            "warningPresentation",
            "observations",
        ],
    }


def _api_key() -> str | None:
    direct = os.getenv("OPENROUTER_API_KEY", "").strip()
    if direct:
        return direct
    key_file = os.getenv("OPENROUTER_API_KEY_FILE", "").strip()
    if not key_file:
        return None
    try:
        return Path(key_file).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def vision_available() -> bool:
    """Report capability without ever exposing the credential to the client."""
    return _api_key() is not None


def cache_identity() -> str:
    model = os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    endpoint = os.getenv("OPENROUTER_API_URL", DEFAULT_ENDPOINT).strip() or DEFAULT_ENDPOINT
    return f"{PROMPT_VERSION}|{model}|{endpoint}|temperature-0|minimal|schema-v1"


def _safe_number(value: object) -> int | None:
    return int(value) if isinstance(value, int | float) and value >= 0 else None


def _failed_run(
    panel: ValidatedPanel,
    *,
    model: str,
    reason: str,
    duration_ms: float,
    raw_response: bytes = b"",
) -> VisionRun:
    digest_input = raw_response or reason.encode("utf-8")
    return VisionRun(
        schema_version="vision-extraction-v1",
        prompt_version=PROMPT_VERSION,
        provider="openrouter",
        requested_model=model,
        response_model="unavailable",
        provider_request_id="unavailable",
        raw_response_sha256=hashlib.sha256(digest_input).hexdigest(),
        panels=(
            VisionPanelExtraction(
                panel_id=panel.panel_id,
                full_text="",
                fields=(),
                observations=(reason,),
            ),
        ),
        duration_ms=duration_ms,
    )


def unavailable_vision_run(panel: ValidatedPanel, reason: str) -> VisionRun:
    model = os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    return _failed_run(panel, model=model, reason=reason, duration_ms=0.0)


def run_openrouter_vision(
    content: bytes,
    panel: ValidatedPanel,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> VisionRun:
    started = time.perf_counter()
    model = os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    key = _api_key()
    if not key:
        return _failed_run(
            panel,
            model=model,
            reason="Connected vision is unavailable because no OpenRouter key is configured.",
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    blind = build_blind_vision_request(panel, content)
    image = blind["panels"][0]["image"]
    payload = {
        "model": model,
        "temperature": 0,
        "reasoning_effort": "minimal",
        "max_tokens": 3000,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": BLIND_EXTRACTION_PROMPT},
                    {"type": "image_url", "image_url": {"url": image}},
                ],
            }
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "ttb_label_extraction",
                "strict": True,
                "schema": _response_schema(panel.panel_id),
            },
        },
    }
    serialized = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        os.getenv("OPENROUTER_API_URL", DEFAULT_ENDPOINT),
        data=serialized,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "X-Title": "TTB Label Verification",
        },
    )
    raw_response = b""
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw_response = response.read()
    except urllib.error.HTTPError as exc:
        raw_response = exc.read()
        return _failed_run(
            panel,
            model=model,
            reason=f"Connected vision returned HTTP {exc.code}; human review is required.",
            duration_ms=(time.perf_counter() - started) * 1000,
            raw_response=raw_response,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return _failed_run(
            panel,
            model=model,
            reason=f"Connected vision failed ({type(exc).__name__}); human review is required.",
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    try:
        envelope = json.loads(raw_response)
        choice = envelope["choices"][0]["message"]["content"]
        if not isinstance(choice, str):
            raise TypeError("Provider content was not a string")
        extraction_payload = json.loads(choice)
        if not isinstance(extraction_payload, dict):
            raise TypeError("Provider extraction was not an object")
        extraction_payload["panelId"] = panel.panel_id
        raw_fields = extraction_payload.get("fields")
        if isinstance(raw_fields, list):
            for raw_field in raw_fields:
                if isinstance(raw_field, dict):
                    raw_field["panelId"] = panel.panel_id
                    raw_box = raw_field.pop("evidenceBox1000", None)
                    if raw_box is not None and not isinstance(raw_box, dict):
                        raise ValueError(
                            "Provider returned a non-object normalized evidence box"
                        )
                    raw_field["modelBoundingBox"] = _normalized_box_to_pixels(
                        raw_box,
                        panel,
                    )
        extraction = VisionPanelExtraction.model_validate(extraction_payload)
        field_keys = [field.field_key for field in extraction.fields]
        if len(field_keys) != len(set(field_keys)):
            raise ValueError("Provider returned duplicate field candidates")
    except (
        KeyError,
        IndexError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        ValidationError,
    ) as exc:
        debug_detail = f": {exc}" if os.getenv("VISION_DEBUG_ERRORS") == "1" else ""
        return _failed_run(
            panel,
            model=model,
            reason=(
                "Connected vision returned an invalid extraction envelope "
                f"({type(exc).__name__}){debug_detail}; human review is required."
            ),
            duration_ms=(time.perf_counter() - started) * 1000,
            raw_response=raw_response,
        )

    usage = envelope.get("usage") if isinstance(envelope, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    cost = usage.get("cost")
    return VisionRun(
        schema_version="vision-extraction-v1",
        prompt_version=PROMPT_VERSION,
        provider="openrouter",
        requested_model=model,
        response_model=str(envelope.get("model") or model),
        provider_request_id=str(envelope.get("id") or "unavailable"),
        raw_response_sha256=hashlib.sha256(raw_response).hexdigest(),
        panels=(extraction,),
        duration_ms=(time.perf_counter() - started) * 1000,
        input_tokens=_safe_number(usage.get("prompt_tokens")),
        output_tokens=_safe_number(usage.get("completion_tokens")),
        estimated_cost_usd=(
            float(cost) if isinstance(cost, int | float) and cost >= 0 else None
        ),
    )
