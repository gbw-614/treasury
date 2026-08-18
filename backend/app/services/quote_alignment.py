from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from app.schemas import FieldKey, LocalizationResult, LocationStatus, OcrRun, OcrToken

ALIGNMENT_VERSION = "ordered-window-v1"
FUZZY_THRESHOLD = 0.76
UNIQUE_MARGIN = 0.05


def _parts(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z0-9]+", value.casefold()))


def _compact(value: str) -> str:
    return "".join(_parts(value))


def _case_sensitive_compact(value: str) -> str:
    return "".join(re.findall(r"[A-Za-z0-9]+", value))


@dataclass(frozen=True)
class Window:
    start: int
    end: int
    tokens: tuple[OcrToken, ...]
    compact: str
    case_sensitive_compact: str
    similarity: float


def _overlap_ratio(left: Window, right: Window) -> float:
    intersection = max(0, min(left.end, right.end) - max(left.start, right.start))
    shortest = min(left.end - left.start, right.end - right.start)
    return intersection / shortest if shortest else 0.0


def align_quote(
    *,
    field_key: FieldKey,
    quote: str,
    panel_id: str,
    ocr_run: OcrRun,
) -> LocalizationResult:
    localization_id = f"loc-{field_key.value}"
    quote_parts = _parts(quote)
    quote_compact = "".join(quote_parts)
    quote_case_sensitive = _case_sensitive_compact(quote)
    searchable = tuple(token for token in ocr_run.tokens if _compact(token.text))
    if not quote_compact or not searchable:
        return LocalizationResult(
            localization_id=localization_id,
            field_key=field_key,
            quote=quote,
            panel_id=panel_id,
            status=LocationStatus.UNAVAILABLE,
            reason="No OCR tokens were available for this evidence quote.",
        )

    target_length = max(1, len(quote_parts))
    minimum_length = max(1, target_length - 4)
    maximum_length = min(len(searchable), target_length + 6)
    windows: list[Window] = []
    for length in range(minimum_length, maximum_length + 1):
        for start in range(len(searchable) - length + 1):
            window_tokens = searchable[start : start + length]
            compact = "".join(_compact(token.text) for token in window_tokens)
            case_sensitive_compact = "".join(
                _case_sensitive_compact(token.text) for token in window_tokens
            )
            if not compact:
                continue
            similarity = SequenceMatcher(None, quote_compact, compact).ratio()
            windows.append(
                Window(
                    start=start,
                    end=start + length,
                    tokens=window_tokens,
                    compact=compact,
                    case_sensitive_compact=case_sensitive_compact,
                    similarity=similarity,
                )
            )

    case_sensitive_exact = [
        window
        for window in windows
        if window.case_sensitive_compact == quote_case_sensitive
    ]
    if case_sensitive_exact:
        distinct: list[Window] = []
        for window in sorted(
            case_sensitive_exact, key=lambda item: (item.start, item.end)
        ):
            if not any(_overlap_ratio(window, prior) >= 0.8 for prior in distinct):
                distinct.append(window)
        if len(distinct) == 1:
            best = distinct[0]
            return LocalizationResult(
                localization_id=localization_id,
                field_key=field_key,
                quote=quote,
                panel_id=panel_id,
                status=LocationStatus.LOCATED,
                accepted_token_ids=tuple(token.token_id for token in best.tokens),
                display_boxes=tuple(token.bounding_box for token in best.tokens),
                similarity=1.0,
                reason=f"Unique case-preserving OCR-token match ({ALIGNMENT_VERSION}).",
            )

    exact = [window for window in windows if window.compact == quote_compact]
    if exact:
        distinct: list[Window] = []
        for window in sorted(exact, key=lambda item: (item.start, item.end)):
            if not any(_overlap_ratio(window, prior) >= 0.8 for prior in distinct):
                distinct.append(window)
        if len(distinct) > 1:
            return LocalizationResult(
                localization_id=localization_id,
                field_key=field_key,
                quote=quote,
                panel_id=panel_id,
                status=LocationStatus.AMBIGUOUS,
                similarity=1.0,
                runner_up_similarity=1.0,
                reason="The evidence quote appears in more than one OCR location.",
            )
        best = distinct[0]
        return LocalizationResult(
            localization_id=localization_id,
            field_key=field_key,
            quote=quote,
            panel_id=panel_id,
            status=LocationStatus.LOCATED,
            accepted_token_ids=tuple(token.token_id for token in best.tokens),
            display_boxes=tuple(token.bounding_box for token in best.tokens),
            similarity=1.0,
            reason=f"Exact ordered OCR-token match ({ALIGNMENT_VERSION}).",
        )

    if not windows:
        return LocalizationResult(
            localization_id=localization_id,
            field_key=field_key,
            quote=quote,
            panel_id=panel_id,
            status=LocationStatus.UNAVAILABLE,
            reason="No OCR window could be compared with the evidence quote.",
        )

    ranked = sorted(windows, key=lambda item: item.similarity, reverse=True)
    best = ranked[0]
    runner = next(
        (
            candidate
            for candidate in ranked[1:]
            if _overlap_ratio(best, candidate) < 0.5
        ),
        None,
    )
    runner_score = runner.similarity if runner else None
    if best.similarity < FUZZY_THRESHOLD:
        return LocalizationResult(
            localization_id=localization_id,
            field_key=field_key,
            quote=quote,
            panel_id=panel_id,
            status=LocationStatus.UNAVAILABLE,
            similarity=best.similarity,
            runner_up_similarity=runner_score,
            reason="No OCR window was similar enough to support a highlight.",
        )
    if runner_score is not None and best.similarity - runner_score < UNIQUE_MARGIN:
        return LocalizationResult(
            localization_id=localization_id,
            field_key=field_key,
            quote=quote,
            panel_id=panel_id,
            status=LocationStatus.AMBIGUOUS,
            similarity=best.similarity,
            runner_up_similarity=runner_score,
            reason="Two distinct OCR regions were similarly plausible matches.",
        )
    return LocalizationResult(
        localization_id=localization_id,
        field_key=field_key,
        quote=quote,
        panel_id=panel_id,
        status=LocationStatus.LOCATED,
        accepted_token_ids=tuple(token.token_id for token in best.tokens),
        display_boxes=tuple(token.bounding_box for token in best.tokens),
        similarity=best.similarity,
        runner_up_similarity=runner_score,
        reason=f"Unique fuzzy OCR-token match ({ALIGNMENT_VERSION}).",
    )
