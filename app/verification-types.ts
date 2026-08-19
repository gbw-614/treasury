export type BeverageCategory = "wine" | "distilled_spirits" | "malt_beverage";
export type ReaderMode = "ocr" | "llm";
export type AdditionalExpectedField = {
  id: string;
  label: string;
  expectedText: string;
  matchMode: "literal_phrase";
};

/** A selected check from the server-side, versioned field library. */
export type FieldLibraryCheck = {
  fieldId: string;
  required: boolean;
  expectedValue: string | null;
};
export type AnalysisStatus = "queued" | "analyzing" | "complete" | "error";
export type DecisionStatus =
  | "awaiting_analysis"
  | "ready_for_decision"
  | "pass"
  | "fail"
  | "needs_review";

export type ReaderCapabilities = {
  visionAvailable: boolean;
  availableReaderModes: ReaderMode[];
};

export type FieldKey =
  | "brand_name"
  | "class_type"
  | "alcohol_content"
  | "proof"
  | "government_warning_heading"
  | "government_warning_body";

export type BoundingBox = {
  x: number;
  y: number;
  width: number;
  height: number;
};

export type ValidatedPanel = {
  panelId: string;
  sourceSha256: string;
  detectedMimeType: "image/jpeg" | "image/png";
  width: number;
  height: number;
  exifOrientation: number | null;
  appliedTransform: string;
  decodedPixelCount: number;
  originalByteCount: number;
  preprocessingVersion: string;
};

export type VisionFieldCandidate = {
  fieldKey: FieldKey;
  rawText: string;
  normalizedValue: string | number | null;
  evidenceQuote: string;
  modelBoundingBox: BoundingBox | null;
  panelId: string;
  legibility: "clear" | "uncertain" | "unreadable";
  uncertainty: string | null;
};

export type VisionTextBlock = {
  blockId: string;
  text: string;
  modelBoundingBox: BoundingBox | null;
  readingOrder: number;
  legibility: "clear" | "uncertain" | "unreadable";
  uncertainty: string | null;
};

export type VisionRun = {
  schemaVersion: "vision-extraction-v1";
  promptVersion: string;
  provider: string;
  requestedModel: string;
  responseModel: string;
  providerRequestId: string;
  rawResponseSha256: string;
  panels: Array<{
    panelId: string;
    fullText: string;
    fields: VisionFieldCandidate[];
    textBlocks: VisionTextBlock[];
    warningPresentation: {
      headingAllCaps: boolean | null;
      headingOnlyBold: boolean | null;
      continuousParagraph: boolean | null;
      separateAndApart: boolean | null;
      legibleContrast: boolean | null;
      textAppearsUnusuallySmall: boolean | null;
      uncertainty: string | null;
    } | null;
    observations: string[];
  }>;
  durationMs: number;
  inputTokens: number | null;
  outputTokens: number | null;
  estimatedCostUsd: number | null;
};

export type OcrToken = {
  tokenId: string;
  text: string;
  boundingBox: BoundingBox;
  confidence: number;
  blockId: string;
  lineId: string;
  readingOrder: number;
};

export type OcrRun = {
  schemaVersion: "ocr-geometry-v1";
  adapterVersion: string;
  engine: string;
  engineVersion: string;
  languageData: string;
  executable: string;
  preprocessingVersion: string;
  panelId: string;
  sourceSha256: string;
  originalWidth: number;
  originalHeight: number;
  transcript: string;
  tokens: OcrToken[];
  exitStatus: number;
  warnings: string[];
  preprocessingDurationMs: number;
  ocrDurationMs: number;
};

export type LocalizationResult = {
  localizationId: string;
  fieldKey: FieldKey;
  quote: string;
  panelId: string;
  status: "located" | "location_ambiguous" | "location_unavailable";
  acceptedTokenIds: string[];
  displayBoxes: BoundingBox[];
  similarity: number | null;
  runnerUpSimilarity: number | null;
  reason: string;
};

export type RuleResult = {
  ruleId: string;
  ruleVersion: string;
  fieldKey: FieldKey;
  applicable: boolean;
  automatedStatus: "matches" | "discrepancy" | "review" | "does_not_apply";
  expectedValue: string | number | null;
  detectedValue: string | number | null;
  evidenceQuote: string | null;
  localizationIds: string[];
  factsConsumed: string[];
  reasonCode: string;
  explanation: string;
  requiresHumanReview: boolean;
};

export type AdditionalRuleResult = {
  fieldId: string;
  label: string;
  matchMode: "literal_phrase" | "text" | "regex" | "warning";
  automatedStatus: "matches" | "discrepancy" | "review" | "does_not_apply";
  expectedValue: string | null;
  detectedValue: string | null;
  evidenceQuote: string | null;
  evidenceBlockIds: string[];
  reasonCode: string;
  explanation: string;
  requiresHumanReview: boolean;
};

export type AnalysisResponse = {
  schemaVersion: "analysis-response-v1";
  analysisId: string;
  mode: "connected";
  readerMode: ReaderMode;
  overallSummary:
    | "no_automated_discrepancy_detected"
    | "needs_review"
    | "automated_discrepancy_detected";
  panels: ValidatedPanel[];
  visionRun: VisionRun;
  ocrRun: OcrRun;
  ocrRuns?: OcrRun[];
  localizations: LocalizationResult[];
  ruleResults: RuleResult[];
  additionalRuleResults?: AdditionalRuleResult[];
  reviewTasks: Array<{
    fieldKey: FieldKey;
    reasonCode: string;
    message: string;
    localizationIds: string[];
  }>;
  stageDurations: Array<{ stage: string; milliseconds: number }>;
  totalDurationMs: number;
};

export type ProcessingStatus = "queued" | "processing" | "complete" | "error";
export type CaseOutcome = "pass" | "fail" | "needs_review";
export type DecisionSource = "automated" | "human_confirmed" | "human_overridden";

export type QueueCase = {
  caseId: string;
  displayName: string;
  createdAt: string;
  updatedAt: string;
  requestSchemaVersion: "verification-request-v1" | "verification-request-v2";
  category: BeverageCategory | null;
  expected: {
    brandName: string | null;
    classType: string | null;
    abvPercent: number | null;
    proof: number | null;
    governmentWarning: {
      heading: string;
      body: string;
    } | null;
    additionalFields: AdditionalExpectedField[];
  } | null;
  checks: FieldLibraryCheck[];
  panel: {
    panelId: string;
    file: string;
    url: string;
    sha256: string;
    mimeType: "image/jpeg" | "image/png";
    width: number;
    height: number;
  };
  panels: Array<QueueCase["panel"]>;
  processingStatus: ProcessingStatus;
  analysisStatus: AnalysisStatus;
  decisionStatus: DecisionStatus;
  automatedOutcome: CaseOutcome | null;
  outcome: CaseOutcome | null;
  decisionSource: DecisionSource | null;
  errorMessage: string | null;
  reviewNote: string | null;
  reviewedAt: string | null;
  duplicateImageCount: number;
  removedAt: string | null;
  createdByUserId: string | null;
  createdByUsername: string | null;
  assignedToUserId: string | null;
  assignedToUsername: string | null;
  reviewedByUserId: string | null;
  reviewedByUsername: string | null;
};

export type QueueUser = {
  userId: string;
  username: string;
  createdAt?: string;
};

export type AuthenticatedUser = QueueUser;

export type QueuePreferences = {
  query: string;
  outcomeFilter: "all" | CaseOutcome;
  reviewFilter: "all" | "attention" | "automated" | "human";
  assignmentFilter: "all" | "mine" | "unassigned";
  showRemoved: boolean;
  reviewWorkspaceFilter: "all" | "not_human_confirmed" | "review_only" | "failed";
};

export type QueueCaseDetail = QueueCase & {
  analysis: AnalysisResponse | null;
};

export type CaseQueueResponse = {
  schemaVersion: "case-queue-v1";
  cases: QueueCase[];
};

/** A read-only entry advertised by the configured S3 source catalog. */
export type SourceCatalogCase = {
  sourceCaseId: string;
  displayName: string;
  category: BeverageCategory | null;
  panelCount: number;
  alreadyImported: boolean;
  expected?: Partial<QueueCase["expected"]>;
  description?: string;
};

export type SourceCatalogResponse = {
  schemaVersion?: string;
  catalogVersion?: string;
  cases: SourceCatalogCase[];
};

export type SourceImportResult = {
  sourceCaseId: string;
  displayName?: string;
  status: "imported" | "skipped" | "error";
  message?: string;
  caseId?: string;
};

export type SourceImportResponse = {
  results: SourceImportResult[];
};

export type SourceCatalogImportJob = {
  schemaVersion: "verification-source-catalog-import-job-v1";
  jobId: string;
  status: "queued" | "running" | "complete" | "failed";
  totalCases: number;
  completedCases: number;
  importedCaseIds: string[];
  duplicateCaseIds: string[];
  issues: Array<{ sourceCaseId: string; code: string; message: string }>;
  errorMessage: string | null;
  createdAt: string;
  updatedAt: string;
  finishedAt: string | null;
};
