# Verification input contract

The product compares label artwork with explicit application values. It is not
a regulatory rules engine and does not translate a COLA class/type code into
the wording that ought to appear on a label.

## Field-library request (v2)

New catalog and application imports should use `verification-request-v2`.
Each selected check references a reviewed, server-side field-library entry;
the case supplies only whether it is required and, where available, an
independent expected value. The reader never receives either.

```json
{
  "schemaVersion": "verification-request-v2",
  "caseReference": "COLA-26189001000380",
  "category": "wine",
  "checks": [
    {"fieldId": "brand_name", "required": true, "expectedValue": "LIVING ROOTS WINE & CO."},
    {"fieldId": "class_type", "required": true, "expectedValue": "BONE-DRY RIESLING"},
    {"fieldId": "alcohol_content", "required": true, "expectedValue": "12.3"},
    {"fieldId": "government_warning", "required": true}
  ],
  "panels": [
    {"panelId": "p01", "file": "front.jpg"},
    {"panelId": "p02", "file": "back.jpg"}
  ],
  "readerMode": "llm"
}
```

`caseReference` is optional operational metadata (for example, a COLA number)
used in the queue and CSV audit report. It is never sent to a reader or used in
the comparison result.

The initial library is versioned in
`backend/app/config/field-library-v1.json` and supports:

- case-insensitive normalized text: `brand_name`, `class_type`,
  `producer_or_bottler`, `country_of_origin`;
- regex statement detection with optional captured-value comparison:
  `alcohol_content`, `proof`, `net_contents`; and
- the canonical `government_warning` check.

For each v2 check, the caller supplies the field ID, whether it is required,
and an independent expected value when the field definition supports one. The
caller also supplies one to six ordered artwork panels and selects the reader
mode. The server validates field IDs against the versioned library; it never
derives expected wording from COLA metadata or model output.

If a regex field has no `expectedValue`, the result checks only that a
configured statement exists. If it has an expected value, the configured regex
must find a statement and its captured value must match. The application may
omit a field entirely when it is not in scope for that case. `v1` remains
accepted for existing queued cases during the transition.

Regex checks examine the complete ordered label set. Repeated occurrences of
one normalized value are allowed, but multiple distinct captured values route
the field to review even when one equals the expected value. This prevents a
multi-product template containing several ABVs, proofs, or net contents from
passing merely because the expected number appears somewhere on the artwork.

For legacy v1 requests only, the caller supplies:

- one beverage category used only as extraction context;
- one to six ordered artwork panels;
- the exact visible brand and class/type wording expected on those panels;
- expected ABV and proof when those values apply, otherwise `null`; and
- the statutory government-warning heading and body when that warning applies,
  otherwise `null`. Callers are responsible for supplying the authoritative
  expected statement; the official reference catalog always uses the statutory
  wording rather than copying an artwork typo; and
- up to 20 optional literal values in `additionalFields` when the application
  explicitly says other text must appear on the label. These values are
  supplied by the caller and are never inferred from model output.

Example:

```json
{
  "schemaVersion": "verification-request-v1",
  "category": "wine",
  "expected": {
    "brandName": "LIVING ROOTS WINE & CO.",
    "classType": "BONE-DRY RIESLING",
    "abvPercent": 12.3,
    "proof": null,
    "governmentWarning": {
      "heading": "GOVERNMENT WARNING:",
      "body": "(1) According to the Surgeon General, women should not drink alcoholic beverages during pregnancy because of the risk of birth defects. (2) Consumption of alcoholic beverages impairs your ability to drive a car or operate machinery, and may cause health problems."
    },
    "additionalFields": [
      {
        "id": "appellation",
        "label": "Appellation",
        "expectedText": "Willamette Valley",
        "matchMode": "literal_phrase"
      }
    ]
  },
  "panels": [
    {"panelId": "p01", "file": "front.jpg"},
    {"panelId": "p02", "file": "back.jpg"}
  ],
  "readerMode": "llm"
}
```

The extraction prompt never receives `expected`. The selected OCR or vision reader
first read the artwork blindly. Deterministic code then compares the extracted
values with this JSON and returns match, discrepancy, or review. A missing or
unreadable detection is review evidence, not proof that required wording is
absent.

In LLM mode, Gemini returns exhaustive literal text blocks in reading order,
with a best-effort box and legibility assessment for each block. It does not
classify blocks as brand, class/type, alcohol content, or other application
fields. The deterministic field-library evaluator selects the actual transcript
span that satisfies each configured check and links that result to its supporting
block or blocks. A matching block marked uncertain or unreadable routes the field
to review; failure to localize an otherwise clear literal match affects the
evidence control but does not change the match itself.

When an ordinary text check has no exact normalized match, the reviewer result
may show the highest-scoring consecutive literal-block span. The fallback uses
RapidFuzz `fuzz.ratio`, requires a score of at least 80, and always remains a
review result. It is explanatory evidence only; it is never accepted as an
automated match and is not used for regex or government-warning checks.

Additional fields use one deliberately narrow rule: the normalized expected
words must occur as one contiguous phrase in the blind reader transcript. A
found phrase matches; a missing phrase routes to review rather than automatic
failure. This keeps custom fields auditable without turning them into a hidden
regulatory ontology.

Government-warning comparison ignores artwork line wrapping and body letter
case while preserving every word and punctuation mark. The heading remains an
exact uppercase comparison. In LLM mode, the vision response separately
assesses whether only the heading is bold relative to the body and whether the
statement is continuous, set apart, and legible. Unknown presentation routes
to review. OCR mode cannot establish boldness and therefore routes warning
presentation to review. Physical type size in millimeters is not certified
from image pixels alone.

Registry metadata such as `TABLE WHITE WINE` is provenance, not automatically
the expected visible class/type. If the artwork instead says `BONE-DRY
RIESLING`, that literal visible designation is the expected comparison value.
Private formula facts are outside this contract and must never be inferred.
