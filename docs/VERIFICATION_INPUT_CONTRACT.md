# Verification input contract

The product compares label artwork with explicit application values. It is not
a regulatory rules engine and does not translate a COLA class/type code into
the wording that ought to appear on a label.

For each case, the caller supplies:

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
