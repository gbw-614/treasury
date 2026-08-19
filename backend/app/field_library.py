"""Versioned, data-backed field definitions and deterministic check evaluators."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

CheckType = Literal["warning", "text", "regex"]


@dataclass(frozen=True)
class RegexPattern:
    id: str
    regex: str
    value_capture: str


@dataclass(frozen=True)
class FieldDefinition:
    id: str
    name: str
    check_type: CheckType
    patterns: tuple[RegexPattern, ...] = ()
    normalize: str = "text"
    heading: str | None = None
    body: str | None = None


@dataclass(frozen=True)
class CheckEvaluation:
    matched: bool
    detected_value: str | None
    evidence_quote: str | None
    reason_code: str
    explanation: str
    evidence_quotes: tuple[str, ...] = ()


def normalize_text(value: str) -> str:
    """Case/punctuation/whitespace-insensitive form used by literal checks."""
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold().replace("&", " and ")))


def _literal_phrase(transcript: str, expected_value: str) -> str | None:
    """Return the actual transcript span whose normalized words match expected."""
    expected_words = tuple(normalize_text(expected_value).split())
    if not expected_words:
        return None
    spans = [
        (match.group().casefold(), match.start(), match.end())
        for match in re.finditer(r"[a-z0-9]+", transcript, flags=re.IGNORECASE)
    ]
    # `&` normalizes to "and". Treat it as that word while retaining its raw span.
    spans.extend(
        ("and", match.start(), match.end())
        for match in re.finditer(r"&", transcript)
    )
    spans.sort(key=lambda item: item[1])
    # Printed line wrapping sometimes divides one statutory word with a
    # hyphen (for example `ACCORD-ING`). Join only when the concatenated
    # fragments exactly equal a word in the expected phrase. This preserves
    # exact-word matching and does not forgive misspellings such as
    # `GOVERNEMENT`.
    expected_word_set = set(expected_words)
    joined_spans: list[tuple[str, int, int]] = []
    index = 0
    while index < len(spans):
        word, start, end = spans[index]
        best: tuple[str, int, int, int] | None = None
        combined = word
        cursor = index
        combined_end = end
        while cursor + 1 < len(spans):
            next_word, next_start, next_end = spans[cursor + 1]
            if not re.fullmatch(r"\s*-\s*", transcript[combined_end:next_start]):
                break
            combined += next_word
            combined_end = next_end
            cursor += 1
            if combined in expected_word_set:
                best = (combined, start, combined_end, cursor)
            if not any(candidate.startswith(combined) for candidate in expected_word_set):
                break
        if best is not None:
            joined_spans.append(best[:3])
            index = best[3] + 1
        else:
            joined_spans.append((word, start, end))
            index += 1
    spans = joined_spans
    for index in range(len(spans) - len(expected_words) + 1):
        window = spans[index : index + len(expected_words)]
        if tuple(item[0] for item in window) == expected_words:
            start = window[0][1]
            end = window[-1][2]
            # Preserve boundary punctuation when it is part of the expected
            # literal. This matters for `GOVERNMENT WARNING:` and the `(1)`
            # opening/final period of the statutory body while leaving normal
            # punctuation-insensitive text matching unchanged.
            stripped = expected_value.strip()
            leading = re.match(r"^[^a-z0-9\s]+", stripped, flags=re.IGNORECASE)
            trailing = re.search(r"[^a-z0-9\s]+$", stripped, flags=re.IGNORECASE)
            if leading and transcript[:start].endswith(leading.group()):
                start -= len(leading.group())
            if trailing and transcript[end:].startswith(trailing.group()):
                end += len(trailing.group())
            return transcript[start:end]
    return None


def _normalize_value(value: str, mode: str) -> str:
    value = value.strip()
    if mode in {"decimal", "decimal_or_range"}:
        return re.sub(r"\s*[-–]\s*", "-", value.replace(",", ".").replace(" ", ""))
    return normalize_text(value)


@lru_cache(maxsize=1)
def field_library() -> dict[str, FieldDefinition]:
    config_path = Path(__file__).with_name("config") / "field-library-v1.json"
    payload = json.loads(config_path.read_text())
    if payload["schemaVersion"] != "ttb-field-library-v1":
        raise ValueError("Unsupported field library schema version")
    definitions: dict[str, FieldDefinition] = {}
    for item in payload["fields"]:
        definition = FieldDefinition(
            id=item["id"], name=item["name"], check_type=item["checkType"],
            patterns=tuple(RegexPattern(p["id"], p["regex"], p["valueCapture"]) for p in item.get("patterns", [])),
            normalize=item.get("normalize", "text"), heading=item.get("heading"), body=item.get("body"),
        )
        if definition.id in definitions:
            raise ValueError(f"Duplicate field-library id: {definition.id}")
        definitions[definition.id] = definition
    return definitions


def get_field_definition(field_id: str) -> FieldDefinition:
    try:
        return field_library()[field_id]
    except KeyError as exc:
        raise ValueError(f"Unknown field-library id: {field_id}") from exc


def evaluate_check(definition: FieldDefinition, transcript: str, expected_value: str | None) -> CheckEvaluation:
    """Evaluate one check. Regex values are never interpolated into patterns."""
    if definition.check_type == "text":
        if not expected_value:
            return CheckEvaluation(False, None, None, "expected_value_required", "A text check requires an expected value.")
        literal = _literal_phrase(transcript, expected_value)
        if literal is not None:
            return CheckEvaluation(True, literal, literal, "expected_phrase_in_transcript", "The expected value appears in the reader transcript.")
        return CheckEvaluation(False, None, None, "expected_phrase_not_found", "The reader transcript did not contain the expected value.")

    if definition.check_type == "regex":
        matches: list[tuple[str, str, str]] = []
        for pattern in definition.patterns:
            for match in re.finditer(pattern.regex, transcript, flags=re.IGNORECASE):
                detected = match.group(pattern.value_capture)
                matches.append((
                    _normalize_value(detected, definition.normalize),
                    detected,
                    match.group(),
                ))
        if matches:
            values: dict[str, tuple[str, str]] = {}
            for normalized, detected, quote in matches:
                current = values.get(normalized)
                if current is None or len(quote) > len(current[1]):
                    values[normalized] = (detected, quote)
            quotes = tuple(item[1] for item in values.values())
            if len(values) > 1:
                detected_values = ", ".join(item[0] for item in values.values())
                return CheckEvaluation(
                    False,
                    detected_values,
                    quotes[0],
                    "multiple_distinct_values",
                    f"Multiple different {definition.name.lower()} values were found: {detected_values}.",
                    quotes,
                )
            detected = next(iter(values.values()))[0]
            quote = quotes[0]
            if expected_value is None:
                return CheckEvaluation(True, detected, quote, "valid_pattern_found", "A configured valid statement was found.", quotes)
            if next(iter(values)) == _normalize_value(expected_value, definition.normalize):
                return CheckEvaluation(True, detected, quote, "pattern_and_value_match", "A configured valid statement was found and its value matches the expected value.", quotes)
            return CheckEvaluation(False, detected, quote, "pattern_value_differs", "A configured statement was found, but its captured value differs from the expected value.", quotes)
        return CheckEvaluation(False, None, None, "required_pattern_not_found", "No configured valid statement was found in the reader transcript.")

    # The statutory text is fixed by the library. An optional input value is
    # intentionally not treated as an alternate warning requirement.
    canonical = " ".join(part for part in (definition.heading, definition.body) if part)
    literal = _literal_phrase(transcript, canonical) if canonical else None
    if literal is not None:
        return CheckEvaluation(True, literal, literal, "canonical_warning_found", "The canonical Government Health Warning Statement appears in the reader transcript.")
    return CheckEvaluation(False, None, None, "canonical_warning_not_found", "The canonical Government Health Warning Statement was not found in the reader transcript.")
