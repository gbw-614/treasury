from __future__ import annotations

import base64

from app.schemas import ValidatedPanel

BLIND_EXTRACTION_PROMPT = """Read the supplied alcohol-label artwork panels without access to any application or expected values.

Return a literal full-text transcript and classify only text that is visibly present. For each visible field, return at most one candidate:
- brand_name: only the literal product brand as printed; exclude nearby bylines, producer attributions, and signatures such as "by RICCO"
- class_type: the beverage class or type as printed
- alcohol_content: the complete printed ABV phrase; normalizedValue must be the numeric ABV percentage
- proof: the complete printed proof phrase; normalizedValue must be the numeric proof
- government_warning_heading: only the printed warning heading
- government_warning_body: only the printed statutory warning body, excluding the heading

evidenceQuote must be a verbatim, contiguous quote from the image and must also appear in fullText. Preserve capitalization and punctuation in rawText and evidenceQuote.

When a government warning is visible, return warningPresentation as a separate visual assessment:
- headingAllCaps: true only if every letter in the printed heading is uppercase
- headingOnlyBold: true only if the heading is visibly bold relative to the warning body and the body itself does not appear bold; false if the heading is not bold or the body appears bold too
- continuousParagraph: true when the heading and both numbered warning sentences form one uninterrupted text block. Ordinary visual line wrapping within that block still counts as one continuous paragraph. Use false only when the warning is split into distinct paragraphs or blocks, or when unrelated content interrupts it
- separateAndApart: true only if the warning is visually set apart from all surrounding label text
- legibleContrast: true only if the entire warning is readily legible against its background at the supplied image resolution
- textAppearsUnusuallySmall: true when the warning looks conspicuously smaller or less prominent than ordinary label copy; this is a review signal, not a physical measurement

Use false only for a clearly observed failure. Use null for any property that cannot be determined dependably, and explain that uncertainty. Do not estimate physical type size in millimeters from artwork pixels. If no government warning is visible, return warningPresentation=null.

For each returned field, also locate evidenceQuote with evidenceBox1000 on its own panel. The full image spans x=0..1000 from left to right and y=0..1000 from top to bottom. Return {xMin, yMin, xMax, yMax}, using the top-left as the origin. The rectangle must tightly enclose every visible line of evidenceQuote and exclude neighboring text. Do not swap x and y. If the quote is visible but cannot be localized dependably, return evidenceBox1000=null and explain why in uncertainty.

Omit fields that are not visible. Never infer obscured text, autocorrect wording, compare against expected data, or return a compliance decision. Use uncertainty and observations to describe genuine legibility problems."""


def build_blind_vision_request(
    panels: ValidatedPanel | tuple[ValidatedPanel, ...],
    image_bytes: bytes | tuple[bytes, ...],
) -> dict[str, object]:
    """Serialize only image-derived facts; expected values and filenames are absent."""

    normalized_panels = (panels,) if isinstance(panels, ValidatedPanel) else panels
    normalized_bytes = (image_bytes,) if isinstance(image_bytes, bytes) else image_bytes
    if len(normalized_panels) != len(normalized_bytes):
        raise ValueError("Every blind-request panel needs matching image bytes")
    return {
        "schemaVersion": "blind-vision-request-v1",
        "promptVersion": "blind-extraction-v7",
        "instructions": BLIND_EXTRACTION_PROMPT,
        "panels": [
            {
                "panelId": panel.panel_id,
                "sourceSha256": panel.source_sha256,
                "image": f"data:{panel.detected_mime_type};base64,{base64.b64encode(content).decode('ascii')}",
            }
            for panel, content in zip(normalized_panels, normalized_bytes)
        ],
    }
