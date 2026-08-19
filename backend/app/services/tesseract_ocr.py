from __future__ import annotations

import csv
import io
import shutil
import subprocess
import time
from functools import lru_cache

from PIL import Image, ImageOps

from app.schemas import BoundingBox, OcrRun, OcrToken, ValidatedPanel

ADAPTER_VERSION = "tesseract-tsv-v1"
DEFAULT_TIMEOUT_SECONDS = 8.0
PREPROCESSING_VERSION = "exif-transpose-rgb-png-v1"
TESSERACT_CONFIG = "eng|psm-11|tsv"


@lru_cache(maxsize=1)
def _executable() -> str | None:
    return shutil.which("tesseract")


@lru_cache(maxsize=1)
def _engine_version() -> str:
    executable = _executable()
    if not executable:
        return "unavailable"
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            check=False,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    first_line = (result.stdout or result.stderr).splitlines()
    return first_line[0].strip() if first_line else "unknown"


def cache_identity() -> str:
    return f"{ADAPTER_VERSION}|{_engine_version()}|{PREPROCESSING_VERSION}|{TESSERACT_CONFIG}"


def _oriented_png(content: bytes) -> bytes:
    with Image.open(io.BytesIO(content)) as source:
        oriented = ImageOps.exif_transpose(source).convert("RGB")
        output = io.BytesIO()
        oriented.save(output, format="PNG", optimize=False)
        return output.getvalue()


def _empty_run(
    panel: ValidatedPanel,
    *,
    exit_status: int,
    warning: str,
    preprocessing_duration_ms: float,
    ocr_duration_ms: float,
) -> OcrRun:
    return OcrRun(
        schema_version="ocr-geometry-v1",
        adapter_version=ADAPTER_VERSION,
        engine="tesseract",
        engine_version=_engine_version(),
        language_data="eng",
        executable=_executable() or "unavailable",
        preprocessing_version=PREPROCESSING_VERSION,
        panel_id=panel.panel_id,
        source_sha256=panel.source_sha256,
        original_width=panel.width,
        original_height=panel.height,
        transcript="",
        tokens=(),
        exit_status=exit_status,
        warnings=(warning,),
        preprocessing_duration_ms=preprocessing_duration_ms,
        ocr_duration_ms=ocr_duration_ms,
    )


def run_tesseract(
    content: bytes,
    panel: ValidatedPanel,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> OcrRun:
    preprocess_started = time.perf_counter()
    try:
        prepared = _oriented_png(content)
    except (OSError, ValueError) as exc:
        duration = (time.perf_counter() - preprocess_started) * 1000
        return _empty_run(
            panel,
            exit_status=2,
            warning=f"OCR preprocessing failed: {type(exc).__name__}",
            preprocessing_duration_ms=duration,
            ocr_duration_ms=0,
        )
    preprocessing_duration_ms = (time.perf_counter() - preprocess_started) * 1000

    executable = _executable()
    if not executable:
        return _empty_run(
            panel,
            exit_status=127,
            warning="Tesseract executable is unavailable.",
            preprocessing_duration_ms=preprocessing_duration_ms,
            ocr_duration_ms=0,
        )

    ocr_started = time.perf_counter()
    try:
        result = subprocess.run(
            [executable, "stdin", "stdout", "-l", "eng", "--psm", "11", "tsv"],
            input=prepared,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return _empty_run(
            panel,
            exit_status=124,
            warning=f"Tesseract exceeded its {timeout_seconds:g}s deadline.",
            preprocessing_duration_ms=preprocessing_duration_ms,
            ocr_duration_ms=(time.perf_counter() - ocr_started) * 1000,
        )
    except OSError as exc:
        return _empty_run(
            panel,
            exit_status=126,
            warning=f"Tesseract could not start: {type(exc).__name__}",
            preprocessing_duration_ms=preprocessing_duration_ms,
            ocr_duration_ms=(time.perf_counter() - ocr_started) * 1000,
        )
    ocr_duration_ms = (time.perf_counter() - ocr_started) * 1000

    if result.returncode != 0:
        return _empty_run(
            panel,
            exit_status=result.returncode,
            warning="Tesseract returned a nonzero exit status.",
            preprocessing_duration_ms=preprocessing_duration_ms,
            ocr_duration_ms=ocr_duration_ms,
        )

    decoded = result.stdout.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(decoded), delimiter="\t")
    tokens: list[OcrToken] = []
    warnings: list[str] = []
    for row in reader:
        text = (row.get("text") or "").strip()
        if row.get("level") != "5" or not text:
            continue
        try:
            x = int(row["left"])
            y = int(row["top"])
            width = int(row["width"])
            height = int(row["height"])
            confidence = max(0.0, min(100.0, float(row["conf"])))
        except (KeyError, TypeError, ValueError):
            warnings.append("Skipped a malformed Tesseract TSV word row.")
            continue
        if width <= 0 or height <= 0:
            continue
        if x < 0 or y < 0 or x + width > panel.width or y + height > panel.height:
            warnings.append("Skipped a Tesseract word box outside the oriented image.")
            continue
        tokens.append(
            OcrToken(
                token_id=f"{panel.panel_id}-t{len(tokens) + 1:04d}",
                text=text,
                bounding_box=BoundingBox(x=x, y=y, width=width, height=height),
                confidence=confidence,
                block_id=f"b{row.get('block_num', '0')}",
                line_id=(
                    f"b{row.get('block_num', '0')}-p{row.get('par_num', '0')}-"
                    f"l{row.get('line_num', '0')}"
                ),
                reading_order=len(tokens),
            )
        )

    transcript = " ".join(token.text for token in tokens)
    if not tokens:
        warnings.append("Tesseract returned no word tokens.")
    return OcrRun(
        schema_version="ocr-geometry-v1",
        adapter_version=ADAPTER_VERSION,
        engine="tesseract",
        engine_version=_engine_version(),
        language_data="eng",
        executable=executable,
        preprocessing_version=PREPROCESSING_VERSION,
        panel_id=panel.panel_id,
        source_sha256=panel.source_sha256,
        original_width=panel.width,
        original_height=panel.height,
        transcript=transcript,
        tokens=tuple(tokens),
        exit_status=0,
        warnings=tuple(dict.fromkeys(warnings)),
        preprocessing_duration_ms=preprocessing_duration_ms,
        ocr_duration_ms=ocr_duration_ms,
    )
