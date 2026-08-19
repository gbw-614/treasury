from __future__ import annotations

import base64

from app.schemas import ValidatedPanel

BLIND_EXTRACTION_PROMPT = """Read the supplied alcohol-label artwork without access to any application or expected values.

Transcribe all visible text as literal textBlocks in natural reading order. Use one block per coherent printed line or tightly grouped multiline passage. Preserve the visible spelling, numbers, punctuation, capitalization, and accents. Do not classify any text as a brand, class/type, alcohol value, producer, or other semantic role. The server—not you—will apply deterministic field definitions to the transcript.

For every textBlock:
- text must be a verbatim transcription of visible text; never autocorrect or expand abbreviations
- evidenceBox1000 must tightly enclose exactly that printed block, excluding neighboring text
- use legibility=uncertain or unreadable rather than guessing obscured characters

The full image spans x=0..1000 from left to right and y=0..1000 from top to bottom. Use {xMin, yMin, xMax, yMax}, with the top-left as the origin. Do not swap x and y. If text is visible but cannot be localized dependably, return evidenceBox1000=null and explain why.

When a government warning is visible, return warningPresentation as a separate visual assessment:
- headingAllCaps: true only if every letter in the printed heading is uppercase
- headingOnlyBold: true only if the heading is visibly bold relative to the warning body and the body itself does not appear bold; false if the heading is not bold or the body appears bold too
- continuousParagraph: true when the heading and both numbered warning sentences form one uninterrupted text block. Ordinary visual line wrapping within that block still counts as one continuous paragraph. Use false only when the warning is split into distinct paragraphs or blocks, or when unrelated content interrupts it
- separateAndApart: true only if the warning is visually set apart from all surrounding label text
- legibleContrast: true only if the entire warning is readily legible against its background at the supplied image resolution
- textAppearsUnusuallySmall: true when the warning looks conspicuously smaller or less prominent than ordinary label copy; this is a review signal, not a physical measurement

Use false only for a clearly observed failure. Use null for any property that cannot be determined dependably, and explain that uncertainty. Do not estimate physical type size in millimeters from artwork pixels. If no government warning is visible, return warningPresentation=null.

Never infer obscured text, compare against expected data, decide what a field means, or return a compliance decision. Use observations only for genuine image/legibility issues."""


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
        "promptVersion": "blind-extraction-v8",
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
