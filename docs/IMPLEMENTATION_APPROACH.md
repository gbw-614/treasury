# Approach: AI-Powered Alcohol Label Verification Prototype

## Overview

This proof of concept is designed to accelerate alcohol-label review by comparing supplied label artwork with supplied, structured application data. It focuses on the repetitive part of review: locating required statements in one or more pieces of artwork, comparing them with expected values, presenting the supporting evidence, and routing uncertain cases to a human reviewer.

The prototype deliberately does **not** begin with a raw COLA application form. A case arrives with application JSON that already identifies the checks to perform and, where applicable, the expected values. Producing that JSON from a real COLA form would require a separate intake and regulatory-interpretation system. That work is outside this project's scope.

The core design separates **text extraction** from **decision-making**:

1. A text extractor returns image-derived evidence from the artwork.
2. A deterministic field comparison engine applies versioned checks to that evidence.
3. A human reviewer resolves anything uncertain and may override any automated result.

Two text-extraction paths support different operating environments:

- **LLM vision** uses Gemini through OpenRouter and is better suited to glare, rotation, perspective, low contrast, curved bottles, stylized text, and other difficult imagery.
- **Local OCR** uses Tesseract with local preprocessing and makes no outbound connection. It is less capable on difficult images and cannot reliably establish warning-heading boldness or other visual presentation details, so it produces more conservative `Needs Review` outcomes.

Cases can be imported in batches, processed in the background, reviewed as results become available, assigned to or released by human reviewers, and exported as a report. This avoids forcing the reviewer through a slow one-case-at-a-time scanning workflow even when an individual extraction takes more than five seconds.

### Tools used

I did not write the project code by hand. I used Codex to generate the code, then reviewed it with Claude Code and also on my own in an IDE.

---

## 1. Problem framing

A large portion of label review consists of structured comparison:

- Does the displayed brand match the application?
- Is the expected alcohol-content statement present?
- Is the net-contents statement correct?
- Is the Government Health Warning wording present and formatted appropriately?

These checks are repetitive, but the overall review process still requires judgment. Images may be blurry or distorted. Formatting requirements may be difficult to establish mechanically. A difference in capitalization may be harmless for one field and disqualifying for another. The appropriate goal is therefore not to remove the reviewer. It is to let the reviewer spend less time searching and more time deciding.

### Design goals

The prototype is optimized around the following stakeholder needs:

| Need | Design response |
|---|---|
| Reduce manual visual comparison | Run deterministic field checks against extracted label text. |
| Avoid a slow, one-at-a-time scanning workflow | Support batch import, background processing, progressive results, and cached extraction output. |
| Operate where outbound inference calls may be blocked | Provide a local Tesseract path with conservative outcomes. |
| Handle glare, rotation, perspective, and imperfect photographs | Provide an LLM vision path using Gemini that can process harder images. |
| Preserve nuance and reviewer judgment | Route uncertainty to review and permit human reviewers to confirm or override automated outcomes. |
| Enforce strict Government Warning wording and presentation | Give the warning its own check type rather than treating it as a generic fuzzy text match. |
| Make findings explainable | Show the label text used by the matcher and, when possible, its location in the artwork. |

---

## 2. Starting assumptions

### 2.1 Structured application data already exists

The prototype starts from the assumption that structured application data already exists. It does **not** begin with a raw COLA application form.

A **case** contains:

- structured application JSON
- one or more artwork images
- grouping information for panels when the application contains multiple pieces of artwork.

The application JSON is authoritative for this prototype. It identifies the checks to perform, supplies expected values where needed, and includes the COLA application number used to link results back to the source application.

Converting a real COLA submission into this structured contract would require a separate intake and regulatory-interpretation process. This prototype assumes that work has already happened upstream and focuses on what happens once the structured case data and artwork are available.

### 2.2 Assumed review workflow

I also made an explicit workflow assumption for the proof of concept. One or more human reviewers work from a shared case queue with visibility into the cases being worked by the team. Cases can be assigned between reviewers, and imported cases are assigned to the importing reviewer by default.

A reviewer can import one or more cases from a local directory. A batch may contain a single case or many cases. Once imported, cases enter the work queue and are processed in the background. Reviewers can work through the full queue or focus only on cases the system flags for attention as results become available.

For each case, the reviewer can approve or reject the outcome, including overriding an automated result when human judgment differs from the system. After review, the reviewer can generate a report containing the outcome for each case. Report entries are linked back to the COLA application number carried in the structured application JSON.

---

## 3. Domain model

### Case

The unit of work. A case combines an application contract with one or more artwork images, its machine-generated evidence, and any reviewer disposition.

### Application contract

The authoritative, case-specific description of what the verifier should check. It selects entries from the versioned field library. It never supplies executable rules, and the reader never receives its expected values.

`caseReference` is optional operational metadata, normally a public TTB COLA identifier such as `COLA-26189001000380`. It supports queue search, audit exports, and traceability to a source record. It is not evidence, is not sent to an OCR or LLM reader, and is never used to infer expected label wording.

```json
{
  "schemaVersion": "verification-request-v2",
  "caseReference": "COLA-26189001000380",
  "category": "wine",
  "checks": [
    {
      "fieldId": "brand_name",
      "required": true,
      "expectedValue": "LIVING ROOTS WINE & CO."
    },
    {
      "fieldId": "alcohol_content",
      "required": true,
      "expectedValue": "12.3"
    },
    {
      "fieldId": "government_warning",
      "required": true
    }
  ],
  "humanJudgment": [
    "Confirm that the displayed alcohol content is appropriate for this product."
  ]
}
```

A selected check may have an expected value or be presence-only. For example, when alcohol content is required but the authoritative application value is unavailable, omit `expectedValue`. The verifier then looks for a recognizable alcohol-content statement but does not claim that a particular percentage is correct.

Fields that are not applicable are not selected. This accommodates imports, proof statements, and product-specific differences without embedding a broad category rules engine in the verifier.

For a case without a public COLA record, `caseReference` may be omitted or may contain the caller’s stable application identifier. It still remains metadata, not a source of expected values.

### Artwork and panels

A case contains one to six ordered artwork images. Each image is independently readable and highlightable. A panel is the ordered image record used during review. Its `panelId`, filename, hash, dimensions, and media type preserve provenance and allow evidence to point to the correct asset.

Multiple images can represent the front, back, neck, or other parts of one label set. The application contract decides which checks apply to the label set. The verifier does not infer physical-side relationships from a COLA ID.

### Evidence

Evidence is literal, image-derived reader output. Depending on the selected reader, it can include:

- transcript blocks, lines, or words
- bounding boxes on a specific artwork panel
- reader confidence, legibility, or uncertainty
- visible capitalization, punctuation, and line breaks
- Government Warning presentation observations.

Evidence is not a field result. The reader reports what it can see. The deterministic matcher applies the selected field-library check to that evidence.

### Automated result

The deterministic result for a selected check, with its expected value (when provided), detected literal evidence, status, explanation, and evidence links. Results may be a match, a discrepancy, or a review request. A missing or uncertain extraction is review evidence, not proof that label text is absent.

At case level, the automated outcome summarizes the selected check results. It does not replace reviewer judgment, especially for visual requirements such as type weight that local OCR cannot establish.

### Reviewer disposition

The reviewer’s final decision after inspecting the application contract, machine result, and artwork evidence. The reviewer may confirm or override the automated outcome and may attach a note. The system records the authenticated reviewer, timestamp, and final disposition for audit export.

---

## 4. System overview

A case enters through local batch import or the deployed sample catalog. FastAPI validates the case and stores its metadata and queue state in SQLite. Background processing first checks the recognition cache, then sends uncached artwork through the selected text-extraction path. The resulting transcript and evidence go to the deterministic field comparison engine. Completed results become available to human reviewers while later cases continue processing, and reviewer dispositions can then be exported in the work-queue report.

### Application stack

- **React and TypeScript** provide the browser interface: work queue, file and catalog import, evidence viewer, review actions, settings, and CSV-report download.
- **FastAPI (Python)** provides the API, authentication, queue orchestration, file validation, text-extraction adapters, caching, and deterministic comparison logic.
- **SQLite** stores users, sessions, preferences, queue state, review decisions, cached recognition results, and case metadata. Artwork is stored on the persistent application volume.
- **Tesseract OCR** provides the local OCR extractor. It runs with local image preprocessing and does not require outbound network access.
- **Gemini vision via OpenRouter** provides the LLM vision extractor. It receives artwork only. Expected application values are excluded from the extraction prompt.
- **The deterministic field matcher** applies configured checks to the extractor’s literal transcript. The extractor returns text and evidence. It does not decide whether a value matches.
- **S3**, exposed through **CloudFront**, stores the public sample-case catalog used for evaluation and demos. The application imports from a fixed manifest, verifies its assets, and snapshots selected cases locally.
- **Docker Compose** packages the React build, FastAPI service, Tesseract runtime, and persistent data volume for repeatable local use.

### Deployment topology

The proof of concept runs as a single Dockerized application on an **AWS EC2 instance**. The container runs Uvicorn/FastAPI and serves the compiled React application from the same image.

**Caddy** sits in front of the application as the HTTPS reverse proxy. It terminates TLS for the public domain, forwards traffic to the application container, and enables secure HTTP-only session cookies in production.

```text
Browser
  → Caddy (TLS / reverse proxy)
  → Docker container on EC2
      → FastAPI / Uvicorn
      → compiled React UI
      → Tesseract
      → SQLite + artwork persistent volume
      → OpenRouter / Gemini, only in LLM vision mode

Public sample catalog
  → CloudFront
  → private S3 catalog objects
```

SQLite and in-process background processing are appropriate for this proof of concept: they keep local setup and deployment simple while supporting a small number of concurrent reviewers. A larger production deployment would move queue coordination, persistent data, and artwork storage to shared services—for example PostgreSQL, object storage, and dedicated background workers—before adding multiple application replicas.

---

## 5. Text extraction

The prototype supports two ways to extract text and visual evidence from label artwork. Both feed the same field comparison engine.

### 5.1 Local OCR: Tesseract

Tesseract runs locally and makes no outbound connection. It returns:

- OCR text
- word-level bounding boxes
- word confidence values
- output from local preprocessing such as orientation correction, scaling, and contrast handling.

Local OCR supports restricted networks and environments where third-party inference is unavailable. It can establish wording and geometry, but it cannot reliably determine visual properties such as whether the Government Warning heading is bold relative to the body. When a selected check depends on evidence local OCR cannot establish, the result is routed to `Needs Review`.

### 5.2 LLM vision: Gemini through OpenRouter

The LLM path sends **only artwork** to Gemini through OpenRouter. Expected application values remain in the deterministic application layer and are not supplied to the model.

Gemini returns image-derived evidence including:

- a transcript divided into blocks or lines
- a bounding box for each block
- visible capitalization, punctuation, and line breaks
- explicit uncertainty when text cannot be read dependably
- observations about Government Warning presentation, including uppercase heading, heading boldness relative to the body, legibility and contrast, separation from surrounding text, and paragraph continuity.

This path is useful for harder images, including glare, uneven lighting, perspective, rotation, curved-bottle distortion, low-contrast or stylized text, and unusual layouts. Gemini is not asked to decide what a field means or whether a case passes. Its role is to report visible evidence.

The active text-extraction method is selected in settings. If no OpenRouter key is configured, the LLM option is unavailable.

---

## 6. Field comparison engine

Both text-extraction paths feed the same field-library matcher.

### Check types

| Check type | Behavior |
|---|---|
| `text` | Finds an expected literal phrase after normalizing configured differences such as case, whitespace, and punctuation. Expected words must remain consecutive and in order. |
| `regex` | Finds a valid structured statement, captures its value, normalizes it, and optionally compares it with an expected value. |
| `warning` | Applies the canonical Government Health Warning wording and the warning-specific presentation logic supported by the selected text-extraction method. |

A normalized text check is intentionally not the same as semantic equivalence. For example, expected `RED RIDGE DISTILLING` may match `RED RIDGE DISTILLING CO.` when the expected phrase occurs contiguously, but it does not match scattered appearances of those words in unrelated parts of the transcript.

### Initial field library

| Field ID | Type | Initial behavior |
|---|---|---|
| `brand_name` | `text` | Match the supplied brand phrase. |
| `class_type` | `text` | Match the supplied class/type phrase. |
| `alcohol_content` | `regex` | Find a complete alcohol-content statement, capture ABV, and compare when an expected value is supplied. |
| `proof` | `regex` | Find a proof statement and capture the proof value. |
| `net_contents` | `regex` | Find a quantity and unit, normalize the unit, and compare the value. |
| `producer_or_bottler` | `text` | Match the supplied producer/bottler statement, including an address when supplied. |
| `country_of_origin` | `text` | Match the supplied country-of-origin statement. |
| `government_warning` | `warning` | Check canonical wording and supported presentation requirements. |

### Structured fields

Regular-expression checks recognize complete statements rather than arbitrary nearby numbers. Supported alcohol-content forms initially include patterns such as:

- `13.5% Alc. by Vol.`
- `40% Alc/Vol`
- `Alc. 13.5% by Vol.`
- `Alcohol 4.1% by Volume`
- decimal-comma forms such as `11,5% Alc. by Vol.`
- configured ranges where the field library explicitly permits them.

Proof patterns initially include number-first forms such as `94 PROOF`, `(30 PROOF)`, and `80 (PROOF)`. Net-contents patterns recognize a quantity and unit, including `750 mL`, `1 L`, and supported fluid-ounce forms.

### Government Health Warning

The Government Health Warning receives a dedicated rule because its wording and presentation requirements are materially stricter than ordinary brand or producer text.

The engine checks:

- the `GOVERNMENT WARNING:` heading
- canonical body wording and punctuation
- an uppercase heading
- a heading that is bold relative to a body that is not bold, when the active text-extraction method can supply that evidence
- normalized line wrapping and repeated whitespace
- other supported presentation inputs, including paragraph continuity and separation from surrounding text.

Body capitalization alone is not treated as a wording failure. With LLM vision, Gemini's visual observations are inputs to the warning rule. With local OCR, presentation details that cannot be established are reported as uncertain for reviewer resolution.

This prototype does not claim to measure every regulatory property from pixels. In particular, physical type size and definitive legal legibility measurements remain outside the automated result.

---

## 7. Result semantics and human review

The engine supports three case outcomes.

### `Passed`

All selected required checks are satisfied, the evidence is sufficiently clear, and no required human-judgment item remains unresolved.

### `Needs Review`

The system found uncertainty that a person must resolve. Examples include:

- weak or ambiguous OCR evidence
- difficult image quality
- a closest-match candidate that is not an exact deterministic match
- a warning-presentation property that local OCR cannot establish
- a question explicitly listed in `humanJudgment`
- a panel relationship that cannot be determined mechanically.

An unresolved required human-judgment item keeps the case in `Needs Review` even when every automated field check passes.

### `Failed`

A required check has a clear deterministic failure supported by sufficiently strong evidence. The implementation should remain conservative. Uncertainty belongs in `Needs Review`, while `Failed` is reserved for a clear rule violation or missing required statement rather than a low-confidence guess.

### Reviewer actions

The review screen allows a human reviewer to:

- inspect all cases or filter to cases requiring review
- see which image and field require attention
- compare the expected value with the literal detected text
- zoom to the exact match or best candidate region
- see an explicit message when no candidate was found
- confirm the automated outcome
- override it as passed or failed.

A reviewer may also inspect and override a machine pass or failure. The exported report should preserve the automated result separately from the final reviewer disposition so that human intervention is visible rather than silently replacing the machine output.

If a text match is valid but usable geometry is unavailable, the interface should state `MATCH · location unavailable`. It should not imply that there is no evidence merely because a zoom control cannot be enabled.

---

## 8. End-to-end workflow

### 8.1 Import

The prototype provides two intake paths.

**Local batch import** accepts one or more cases from a local directory. Each package includes application JSON and its artwork. Sample directories are included so evaluators can exercise the batch workflow locally.

**Catalog import** is available in the deployed application. Evaluation cases are stored in an S3-backed catalog so a reviewer can test the prototype without first constructing a local directory. This catalog is a demonstration convenience, not the proposed real-world intake architecture.

In a production environment, cases would more likely arrive through an upstream government system or integration that produces the agreed structured contract.

### 8.2 Queue and assignment

Imported cases are assigned to the importing human reviewer by default. A human reviewer can release a case to the shared queue so that another human reviewer can claim or review it.

This provides a simple model for multiple reviewers without requiring a full workforce-management system in the proof of concept.

### 8.3 Background processing

After import and validation, cases enter the work queue and text extraction begins in the background. The system checks the evidence cache before invoking an extractor.

The human reviewer does not have to wait for the entire batch to finish. Completed cases become reviewable while later cases continue processing.

### 8.4 Progressive review

The human reviewer can begin reviewing as soon as results are available. This is a key response to the stakeholder's performance concern: the workflow avoids serializing human work behind one slow image request.

The reviewer can choose to:

- review the full assigned queue
- focus only on `Needs Review` and failed cases.

### 8.5 Resolution and reporting

After resolving the queue, the human reviewer generates a report with each case's outcome. A useful report includes, where available:

- case identifier
- selected text-extraction method
- field-library version
- automated field and case outcomes
- literal evidence or concise evidence references
- unresolved issues
- reviewer overrides
- final disposition.

The report can be saved to disk for evaluation or downstream use.

---

## 9. A few final notes

### Local OCR choice

I compared Tesseract, EasyOCR, and PaddleOCR across **1,153 approved COLA cases comprising 1,884 source-artwork panels**, measuring per-case latency and recovery of supplied registry phrases from each transcript. Tesseract provided the best practical local speed-and-recovery trade-off.

I then evaluated Tesseract preprocessing and recognition parameters and selected grayscale input, a 1,600-pixel width cap, 2× contrast enhancement, Tesseract LSTM / OEM 1, and sparse-text page segmentation (PSM 11). That configuration recovered **56.5% of primary phrases** with a **1.35-second median case time**.

These are **transcript-recovery results**, not field-classification accuracy, full-transcript precision, or label-compliance scores. The source artwork also does not represent photographed-label conditions such as glare, curvature, perspective distortion, or blur.

### Public-record validation example

Testing against public COLA records surfaced what I describe as **a discrepancy in artwork attached to an approved COLA**.

That observation is useful because it shows that evidence-focused comparison can draw a human reviewer's attention to an issue in real public-record material. It should not be expanded into a conclusion that the prototype discovered an illegally marketed product. The public record alone does not establish which artwork was used in commerce or resolve every legal and factual circumstance surrounding the approval.

Source references for that example:

- [Official TTB COLA record](https://www.ttbonline.gov/colasonline/viewColaDetails.do?action=publicDisplaySearchBasic&ttbid=26195001000511)
- [Official application and approval certificate](https://www.ttbonline.gov/colasonline/viewColaDetails.do?action=publicFormDisplay&ttbid=26195001000511)

The defensible takeaway is that the prototype surfaced a discrepancy worth human attention, which is precisely the role the system is designed to play.

### Security and deployment

This is a proof of concept, not a production federal security architecture. I did not attempt to design the full authorization, records-management, retention, or compliance model for a production deployment. The deployed AWS instance follows standard prototype security practices, including HTTPS termination through Caddy and secure HTTP-only session cookies. A production deployment would require a separate security and compliance review.
