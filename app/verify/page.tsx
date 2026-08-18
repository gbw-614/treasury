import {
  AlertTriangle,
  Bot,
  CheckCircle2,
  CircleAlert,
  CircleDot,
  ClipboardList,
  Database,
  Eye,
  FlaskConical,
  Info,
  Layers3,
  LoaderCircle,
  Menu,
  Percent,
  ScanText,
  Settings2,
  ShieldCheck,
  Tag,
  ThumbsDown,
  ThumbsUp,
  Trash2,
  UserCheck,
  LogOut,
  UserRound,
  Wine,
  XCircle,
} from "lucide-react";
import { useCallback, useEffect, useId, useMemo, useState } from "react";
import EvidenceViewer, {
  type EvidencePanel,
  type EvidenceRegion,
} from "../components/evidence-viewer";
import type {
  AdditionalExpectedField,
  AdditionalRuleResult,
  AnalysisResponse,
  AuthenticatedUser,
  BeverageCategory,
  CaseOutcome,
  CaseQueueResponse,
  FieldKey,
  QueueCase,
  QueueCaseDetail,
  QueuePreferences,
  ReaderCapabilities,
  ReaderMode,
  RuleResult,
} from "../verification-types";
import WorkQueue from "./work-queue";
import { previewBatchImportFiles, parseCaseImport, type BatchImportPreview } from "./import-case";
import CaseEntryForm from "./case-entry-form";
import CatalogImport from "./catalog-import";

const API_URL = import.meta.env.VITE_VERIFICATION_API_URL ?? "";

const DEFAULT_QUEUE_PREFERENCES: QueuePreferences = {
  query: "", outcomeFilter: "all", reviewFilter: "all", assignmentFilter: "all", showRemoved: false,
};

const CANONICAL_WARNING = {
  heading: "GOVERNMENT WARNING:",
  body: "(1) According to the Surgeon General, women should not drink alcoholic beverages during pregnancy because of the risk of birth defects. (2) Consumption of alcoholic beverages impairs your ability to drive a car or operate machinery, and may cause health problems.",
};

type VerificationRequest = {
  schemaVersion: "verification-request-v1";
  category: BeverageCategory;
  expected: {
    brandName: string | null;
    classType: string | null;
    abvPercent: number | null;
    proof: number | null;
    governmentWarning: { heading: string; body: string } | null;
    additionalFields: AdditionalExpectedField[];
  };
  panels: Array<{ panelId: string; file: string }>;
};

type RecognitionCacheStats = {
  schemaVersion: "recognition-cache-stats-v1";
  ocrEntries: number;
  llmEntries: number;
  totalEntries: number;
};

function LoginScreen({ onLogin, busy, error, conflict, onReplace }: {
  onLogin: (username: string, password: string) => void;
  busy: boolean;
  error: string;
  conflict: boolean;
  onReplace: () => void;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  return <main className="login-screen">
    <form className="login-card" onSubmit={(event) => { event.preventDefault(); onLogin(username, password); }}>
      <ShieldCheck size={28} /><span className="eyebrow">Regulatory operations</span>
      <h1>Label verification</h1>
      <p>Sign in to access the shared review queue.</p>
      <label>Username<input autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)} required /></label>
      <label>Password<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required /></label>
      {error && <div className="login-error"><CircleAlert size={16} /> {error}</div>}
      {conflict ? <div className="login-conflict"><p>This account is active in another browser session.</p><button type="button" onClick={onReplace} disabled={busy}>Disconnect other session and sign in</button></div> : <button type="submit" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>}
    </form>
  </main>;
}

const FIELD_LABELS: Record<FieldKey, string> = {
  brand_name: "Brand name",
  class_type: "Class / type",
  alcohol_content: "Alcohol by volume",
  proof: "Proof",
  government_warning_heading: "Warning heading",
  government_warning_body: "Warning body",
};

const RESULT_GROUPS: Array<{ label: string; icon: "identity" | "alcohol" | "warning"; fields: FieldKey[] }> = [
  { label: "Identity", icon: "identity", fields: ["brand_name", "class_type"] },
  { label: "Alcohol content", icon: "alcohol", fields: ["alcohol_content", "proof"] },
  {
    label: "Government warning",
    icon: "warning",
    fields: ["government_warning_heading", "government_warning_body"],
  },
];

const READER_CHOICES: ReadonlyArray<readonly [ReaderMode, string, string]> = [
  ["llm", "LLM vision", "Run Gemini only. Use the model’s returned evidence boxes."],
  ["ocr", "OCR", "Run local Tesseract only. It makes conservative match-or-review decisions from its transcript."],
];

function displayValue(value: string | number | null) {
  if (value === null || value === "") return "—";
  return typeof value === "number" ? String(value) : value;
}

function ResultGridValue({ fieldKey, value, detected = false }: {
  fieldKey: FieldKey;
  value: string | number | null;
  detected?: boolean;
}) {
  const displayed = displayValue(value);
  const tooltipId = useId();
  const className = `verify-rule-value${detected ? " detected" : ""}`;
  if (fieldKey !== "government_warning_body" || displayed === "—") {
    return <span className={className} role="cell" title={String(value ?? "")}>{displayed}</span>;
  }
  return (
    <span className={className} role="cell">
      <button
        className="verify-full-text-trigger"
        type="button"
        aria-describedby={tooltipId}
        aria-label={`${detected ? "Detected" : "Expected"} government warning body; hover or focus to read the full text`}
      >
        Full text <Info size={13} aria-hidden="true" />
        <span className="verify-full-text-tooltip" id={tooltipId} role="tooltip">{displayed}</span>
      </button>
    </span>
  );
}

function unionBoxes(boxes: Array<{ x: number; y: number; width: number; height: number }>) {
  if (!boxes.length) return [];
  const left = Math.min(...boxes.map((box) => box.x));
  const top = Math.min(...boxes.map((box) => box.y));
  const right = Math.max(...boxes.map((box) => box.x + box.width));
  const bottom = Math.max(...boxes.map((box) => box.y + box.height));
  return [{ x: left, y: top, width: right - left, height: bottom - top }];
}

function categoryLabel(category: BeverageCategory) {
  if (category === "distilled_spirits") return "Distilled spirits";
  if (category === "malt_beverage") return "Malt beverage";
  return "Wine";
}

function modelLabel(model: string) {
  const knownModels: Record<string, string> = {
    "google/gemini-3.5-flash": "Gemini 3.5 Flash",
  };
  return knownModels[model] ?? model.split("/").at(-1)?.replaceAll("-", " ") ?? model;
}

function statusLabel(status: RuleResult["automatedStatus"]) {
  if (status === "matches") return "Match";
  if (status === "discrepancy") return "Mismatch";
  if (status === "does_not_apply") return "Does not apply";
  return status.replaceAll("_", " ");
}

function errorMessage(payload: unknown, fallback: string) {
  if (!payload || typeof payload !== "object" || !("detail" in payload)) return fallback;
  const detail = (payload as { detail: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && "message" in detail) {
    return String((detail as { message: unknown }).message);
  }
  if (Array.isArray(detail)) {
    return detail.map((item) => (item && typeof item === "object" && "msg" in item ? String(item.msg) : "Invalid request")).join(" · ");
  }
  return fallback;
}

function needsHumanReview(item: QueueCase) {
  return !item.removedAt && item.outcome === "needs_review" && item.decisionSource === "automated";
}

export default function VerifyPage() {
  const [authState, setAuthState] = useState<"checking" | "login" | "ready">("checking");
  const [currentUser, setCurrentUser] = useState<AuthenticatedUser | null>(null);
  const [loginError, setLoginError] = useState("");
  const [loginBusy, setLoginBusy] = useState(false);
  const [sessionConflict, setSessionConflict] = useState(false);
  const [pendingLogin, setPendingLogin] = useState<{ username: string; password: string } | null>(null);
  const [queuePreferences, setQueuePreferences] = useState<QueuePreferences | null>(null);
  const [activeTab, setActiveTab] = useState<"queue" | "review" | "settings">("queue");
  const [readerMode, setReaderMode] = useState<ReaderMode>("ocr");
  const [readerModeDraft, setReaderModeDraft] = useState<ReaderMode>("ocr");
  const [readerCapabilities, setReaderCapabilities] = useState<ReaderCapabilities>({
    visionAvailable: false,
    availableReaderModes: ["ocr"],
  });
  const [cacheStats, setCacheStats] = useState<RecognitionCacheStats | null>(null);
  const [cacheStatsLoading, setCacheStatsLoading] = useState(false);
  const [cacheClearing, setCacheClearing] = useState(false);
  const [cacheError, setCacheError] = useState("");
  const [category, setCategory] = useState<BeverageCategory>("distilled_spirits");
  const [brandName, setBrandName] = useState("Treasury Sample");
  const [classType, setClassType] = useState("Bourbon Whisky");
  const [abvPercent, setAbvPercent] = useState("45");
  const [proof, setProof] = useState("90");
  const [includeGovernmentWarning, setIncludeGovernmentWarning] = useState(true);
  const [additionalExpectedFields, setAdditionalExpectedFields] = useState<AdditionalExpectedField[]>([]);
  const [file, setFile] = useState<File | null>(null);
  const [additionalFiles, setAdditionalFiles] = useState<File[]>([]);
  const [previewUrl, setPreviewUrl] = useState("");
  const [analysis, setAnalysis] = useState<AnalysisResponse | null>(null);
  const [activeCase, setActiveCase] = useState<QueueCaseDetail | null>(null);
  const [queueCases, setQueueCases] = useState<QueueCase[]>([]);
  const [queueLoading, setQueueLoading] = useState(true);
  const [queueError, setQueueError] = useState("");
  const [queueEntryMode, setQueueEntryMode] = useState<"single" | "batch" | "catalog" | null>(null);
  const [scanningCaseId, setScanningCaseId] = useState("");
  const [removingCaseId, setRemovingCaseId] = useState("");
  const [claimingCaseId, setClaimingCaseId] = useState("");
  const [clearingQueue, setClearingQueue] = useState(false);
  const [autoProcess, setAutoProcess] = useState(true);
  const [caseName, setCaseName] = useState("");
  const [importingCase, setImportingCase] = useState(false);
  const [validatingBatch, setValidatingBatch] = useState(false);
  const [batchPreview, setBatchPreview] = useState<BatchImportPreview | null>(null);
  const [importSummary, setImportSummary] = useState("");
  const [reviewNote, setReviewNote] = useState("");
  const [savingDecision, setSavingDecision] = useState(false);
  const [finishedReviewQueue, setFinishedReviewQueue] = useState(false);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [panelIndex, setPanelIndex] = useState(0);
  const [hoverEvidence, setHoverEvidence] = useState<string[]>([]);
  const [pinnedEvidence, setPinnedEvidence] = useState<string[]>([]);

  const apiFetch = useCallback(async (input: string, init?: RequestInit) => {
    const response = await fetch(input, { ...init, credentials: "include" });
    if (response.status === 401) {
      setCurrentUser(null);
      setAuthState("login");
      setLoginError("Your session has ended. Sign in to continue.");
    }
    return response;
  }, []);

  const loadCacheStats = useCallback(async () => {
    setCacheStatsLoading(true);
    setCacheError("");
    try {
      const response = await apiFetch(`${API_URL}/api/v1/recognition-cache`);
      if (!response.ok) throw new Error("Cache statistics could not be loaded.");
      setCacheStats(await response.json() as RecognitionCacheStats);
    } catch (caught) {
      setCacheError(caught instanceof Error ? caught.message : "Cache statistics could not be loaded.");
    } finally {
      setCacheStatsLoading(false);
    }
  }, [apiFetch]);

  const login = useCallback(async (username: string, password: string, replaceExisting = false) => {
    setLoginBusy(true); setLoginError(""); setSessionConflict(false);
    try {
      const response = await fetch(`${API_URL}/api/v1/auth/login`, {
        method: "POST", credentials: "include", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password, replaceExisting }),
      });
      if (response.status === 409) {
        setPendingLogin({ username, password }); setSessionConflict(true);
        setLoginError("This username is already signed in elsewhere."); return;
      }
      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        throw new Error(errorMessage(payload, "Sign-in failed."));
      }
      const user = (await response.json()) as AuthenticatedUser;
      setCurrentUser(user); setAuthState("ready"); setPendingLogin(null);
    } catch (caught) { setLoginError(caught instanceof Error ? caught.message : "Sign-in failed."); }
    finally { setLoginBusy(false); }
  }, []);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const response = await fetch(`${API_URL}/api/v1/auth/me`, { credentials: "include" });
        if (!response.ok) throw new Error("not signed in");
        const user = (await response.json()) as AuthenticatedUser;
        if (!cancelled) { setCurrentUser(user); setAuthState("ready"); }
      } catch { if (!cancelled) setAuthState("login"); }
    })();
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (authState !== "ready") return;
    let cancelled = false;
    void (async () => {
      const response = await apiFetch(`${API_URL}/api/v1/preferences/queue`);
      if (response.ok && !cancelled) setQueuePreferences({ ...DEFAULT_QUEUE_PREFERENCES, ...(await response.json() as Partial<QueuePreferences>) });
    })();
    return () => { cancelled = true; };
  }, [apiFetch, authState]);

  const saveQueuePreferences = useCallback((preferences: QueuePreferences) => {
    setQueuePreferences(preferences);
  }, []);

  useEffect(() => {
    if (authState !== "ready" || !queuePreferences) return;
    const timer = window.setTimeout(() => {
      void apiFetch(`${API_URL}/api/v1/preferences/queue`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(queuePreferences) });
    }, 550);
    return () => window.clearTimeout(timer);
  }, [apiFetch, authState, queuePreferences]);

  const refreshQueue = useCallback(async () => {
    try {
      const response = await apiFetch(`${API_URL}/api/v1/cases`);
      if (!response.ok) throw new Error(`Work queue unavailable (${response.status})`);
      const payload = (await response.json()) as CaseQueueResponse;
      setQueueCases(payload.cases);
      setQueueError("");
      return payload.cases;
    } catch (caught) {
      setQueueError(caught instanceof Error ? caught.message : "The work queue could not be loaded.");
      return [];
    } finally {
      setQueueLoading(false);
    }
  }, [apiFetch]);

  const fetchCase = useCallback(async (caseId: string) => {
    const response = await apiFetch(`${API_URL}/api/v1/cases/${caseId}`);
    if (!response.ok) throw new Error(`Case unavailable (${response.status})`);
    return (await response.json()) as QueueCaseDetail;
  }, [apiFetch]);

  useEffect(() => {
    if (authState !== "ready") return;
    const frame = window.requestAnimationFrame(() => void refreshQueue());
    return () => window.cancelAnimationFrame(frame);
  }, [authState, refreshQueue]);

  useEffect(() => {
    if (authState !== "ready") return;
    let cancelled = false;
    const loadCapabilities = async () => {
      try {
        const response = await apiFetch(`${API_URL}/api/v1/capabilities`);
        if (!response.ok) throw new Error("capabilities unavailable");
        const capabilities = (await response.json()) as ReaderCapabilities;
        if (cancelled) return;
        setReaderCapabilities(capabilities);
        if (!capabilities.visionAvailable) {
          setReaderMode("ocr");
          setReaderModeDraft("ocr");
        }
      } catch {
        // Fail closed: until the server confirms otherwise, only show OCR.
        if (!cancelled) {
          setReaderCapabilities({ visionAvailable: false, availableReaderModes: ["ocr"] });
          setReaderMode("ocr");
          setReaderModeDraft("ocr");
        }
      }
    };
    void loadCapabilities();
    return () => { cancelled = true; };
  }, [apiFetch, authState]);

  useEffect(() => {
    if (authState !== "ready") return;
    let cancelled = false;
    void (async () => {
      const response = await apiFetch(`${API_URL}/api/v1/preferences/analysis`);
      if (!response.ok || cancelled) return;
      const payload = await response.json() as { readerMode?: unknown };
      const saved = payload.readerMode;
      if (saved !== "ocr" && saved !== "llm" && saved !== "both") return;
      const migrated = saved === "both" ? "llm" : saved;
      const effective = migrated !== "ocr" && !readerCapabilities.visionAvailable ? "ocr" : migrated;
      setReaderMode(effective);
      setReaderModeDraft(effective);
    })();
    return () => { cancelled = true; };
  }, [apiFetch, authState, readerCapabilities.visionAvailable]);

  useEffect(() => {
    if (!queueCases.some((item) => item.processingStatus === "processing")) return;
    const timer = window.setInterval(() => void refreshQueue(), 1800);
    return () => window.clearInterval(timer);
  }, [queueCases, refreshQueue]);

  useEffect(() => {
    if (!activeCase || activeCase.processingStatus !== "processing") return;
    let cancelled = false;
    const poll = async () => {
      try {
        const latest = await fetchCase(activeCase.caseId);
        if (cancelled) return;
        setActiveCase(latest);
        if (latest.analysis) setAnalysis(latest.analysis);
        if (latest.processingStatus === "error") setError(latest.errorMessage ?? "Analysis failed.");
        void refreshQueue();
      } catch (caught) {
        if (!cancelled) setError(caught instanceof Error ? caught.message : "The case could not be refreshed.");
      }
    };
    const timer = window.setInterval(() => void poll(), 1000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [activeCase, fetchCase, refreshQueue]);

  useEffect(() => {
    return () => {
      if (previewUrl) URL.revokeObjectURL(previewUrl);
    };
  }, [previewUrl]);

  const activeEvidenceIds = hoverEvidence.length ? hoverEvidence : pinnedEvidence;
  const evidencePanels = useMemo<EvidencePanel[]>(() => {
    if (!analysis) return [];
    const storedPanels = activeCase?.panels?.length ? activeCase.panels : [activeCase?.panel].filter(Boolean);
    return analysis.panels.map((panel, index) => {
      const stored = storedPanels.find((candidate) => candidate?.panelId === panel.panelId) ?? storedPanels[index];
      return {
        id: panel.panelId,
        file: stored?.file ?? `Panel ${index + 1}`,
        url: stored ? `${API_URL}${stored.url}` : previewUrl,
        width: panel.width,
        height: panel.height,
        format: panel.detectedMimeType.split("/")[1],
      };
    });
  }, [activeCase, analysis, previewUrl]);
  const evidenceRegions = useMemo<EvidenceRegion[]>(
    () => {
      const ocrRegions = (analysis?.localizations ?? []).map((location) => ({
        id: location.localizationId,
        panelId: location.panelId,
        text: location.quote || FIELD_LABELS[location.fieldKey],
        boxes: location.status === "located" ? unionBoxes(location.displayBoxes) : [],
        source: "ocr" as const,
      }));
      const visionRegions = (analysis?.visionRun.panels ?? []).flatMap((panel) =>
        panel.fields.map((candidate, candidateIndex) => ({
          id: `vision-${candidate.fieldKey}-${candidate.panelId}-${candidateIndex}`,
          panelId: candidate.panelId,
          text: candidate.evidenceQuote || FIELD_LABELS[candidate.fieldKey],
          boxes: candidate.modelBoundingBox ? [candidate.modelBoundingBox] : [],
          source: "vision" as const,
        })),
      );
      return [...visionRegions, ...ocrRegions];
    },
    [analysis],
  );
  const localizationsById = useMemo(
    () => new Map((analysis?.localizations ?? []).map((location) => [location.localizationId, location])),
    [analysis],
  );
  const visionEvidenceCandidates = useMemo(
    () => {
      return (analysis?.visionRun.panels ?? []).flatMap((panel) =>
        panel.fields.map((candidate, candidateIndex) => ({
          candidate,
          id: `vision-${candidate.fieldKey}-${candidate.panelId}-${candidateIndex}`,
        })),
      );
    },
    [analysis],
  );
  const visibleRuleResults = useMemo(
    () =>
      (analysis?.ruleResults ?? []).filter(
        (rule) => !(rule.fieldKey === "proof" && rule.expectedValue === null),
      ),
    [analysis],
  );
  const visibleAdditionalRuleResults = analysis?.additionalRuleResults ?? [];
  const allVisibleResults: Array<RuleResult | AdditionalRuleResult> = [...visibleRuleResults, ...visibleAdditionalRuleResults];
  const resultCounts = {
    matches: allVisibleResults.filter((rule) => rule.automatedStatus === "matches").length,
    discrepancies: allVisibleResults.filter((rule) => rule.automatedStatus === "discrepancy").length,
    review: allVisibleResults.filter((rule) => rule.automatedStatus === "review").length,
  };
  const activeClaimedByAnotherReviewer = Boolean(
    activeCase?.assignedToUserId && activeCase.assignedToUserId !== currentUser?.userId,
  );
  const reviewableCaseCount = useMemo(
    () => queueCases.filter(
      (item) => needsHumanReview(item)
        && (!item.assignedToUserId || item.assignedToUserId === currentUser?.userId),
    ).length,
    [currentUser?.userId, queueCases],
  );

  const inspectableOcrIds = (rule: RuleResult) =>
    rule.localizationIds.filter((id) => localizationsById.get(id)?.status === "located");

  const inspectableVisionIds = (rule: RuleResult) => {
    const evidence = visionEvidenceCandidates.find(
      ({ candidate }) =>
        candidate.fieldKey === rule.fieldKey
        && candidate.evidenceQuote === rule.evidenceQuote
        && candidate.modelBoundingBox,
    );
    return evidence ? [evidence.id] : [];
  };

  const showHoveredEvidence = (ids: string[]) => {
    setHoverEvidence(ids);
  };

  const clearHoveredEvidence = () => setHoverEvidence([]);

  const selectUploadedFiles = (selected: File[]) => {
    const [primary, ...additional] = selected;
    setFile(primary ?? null);
    setAdditionalFiles(additional);
    setPreviewUrl(primary ? URL.createObjectURL(primary) : "");
    reset();
  };

  const removePanel = (index: number) => {
    const remaining = [file, ...additionalFiles].filter((item): item is File => Boolean(item));
    remaining.splice(index, 1);
    selectUploadedFiles(remaining);
  };

  const movePanel = (index: number, direction: -1 | 1) => {
    const ordered = [file, ...additionalFiles].filter((item): item is File => Boolean(item));
    const destination = index + direction;
    if (destination < 0 || destination >= ordered.length) return;
    [ordered[index], ordered[destination]] = [ordered[destination], ordered[index]];
    selectUploadedFiles(ordered);
  };

  const createQueuedCase = async (
    request: VerificationRequest,
    artwork: File[],
    displayName: string,
    autoProcessOverride?: boolean,
  ) => {
    const form = new FormData();
    // The server also enforces this. This avoids a stale localStorage choice
    // producing a failed import before the capability request has resolved.
    const effectiveReaderMode = readerCapabilities.visionAvailable ? readerMode : "ocr";
    form.append("request", JSON.stringify({ ...request, readerMode: effectiveReaderMode }));
    artwork.forEach((panel) => form.append("panels", panel));
    form.append("autoProcess", String(autoProcessOverride ?? autoProcess));
    form.append("displayName", displayName);
    const response = await apiFetch(`${API_URL}/api/v1/cases`, {
      method: "POST",
      body: form,
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => null);
      throw new Error(errorMessage(payload, `Analysis failed (${response.status})`));
    }
    return (await response.json()) as QueueCaseDetail;
  };

  const enqueueCase = async (
    request: VerificationRequest,
    artwork: File[],
    displayName: string,
    options: { closeEntry?: boolean; successMessage?: string } = {},
  ) => {
    const closeEntry = options.closeEntry ?? true;
    setSubmitting(true);
    setError("");
    setImportSummary("");
    setAnalysis(null);
    setHoverEvidence([]);
    setPinnedEvidence([]);
    try {
      const created = await createQueuedCase(request, artwork, displayName);
      setActiveCase(created);
      setReviewNote(created.reviewNote ?? "");
      setAnalysis(created.analysis);
      setPanelIndex(0);
      await refreshQueue();
      if (closeEntry) {
        setQueueEntryMode(null);
        if (!autoProcess) setActiveTab("queue");
      } else {
        setImportSummary(options.successMessage ?? `${displayName} was added to the queue.`);
      }
    } catch (caught) {
      setError(
        caught instanceof TypeError
          ? "The local verification service is unavailable. Start the Python API on port 8000 and try again."
          : caught instanceof Error
            ? caught.message
            : "The analysis could not be completed.",
      );
    } finally {
      setSubmitting(false);
    }
  };

  const submit = async () => {
    const artwork = [file, ...additionalFiles].filter((item): item is File => Boolean(item));
    if (!artwork.length) {
      setError("Choose one to six JPEG or PNG label panels first.");
      return;
    }
    if (additionalExpectedFields.some((field) => !field.label.trim() || !field.expectedText.trim())) {
      setError("Every additional field needs both a field name and an expected value on the label.");
      return;
    }
    const additionalLabels = additionalExpectedFields.map((field) => field.label.trim().toLocaleLowerCase());
    if (new Set(additionalLabels).size !== additionalLabels.length) {
      setError("Every additional field needs a unique field name.");
      return;
    }
    await enqueueCase(
      {
        schemaVersion: "verification-request-v1",
        category,
        expected: {
          brandName: brandName.trim() || null,
          classType: classType.trim() || null,
          abvPercent: abvPercent.trim() ? Number(abvPercent) : null,
          proof: category === "distilled_spirits" && proof.trim() ? Number(proof) : null,
          governmentWarning: includeGovernmentWarning ? CANONICAL_WARNING : null,
          additionalFields: additionalExpectedFields.map((field) => ({ ...field, label: field.label.trim(), expectedText: field.expectedText.trim() })),
        },
        panels: artwork.map((panel, index) => ({ panelId: `p${String(index + 1).padStart(2, "0")}`, file: panel.name })),
      },
      artwork,
      caseName.trim() || brandName.trim(),
    );
  };

  const importCaseFiles = async (applicationFile: File, artworkFiles: File[]) => {
    setImportingCase(true);
    setError("");
    setImportSummary("");
    try {
      const parsed = parseCaseImport(JSON.parse(await applicationFile.text()), artworkFiles.map((file) => file.name));
      setCategory(parsed.request.category);
      setBrandName(parsed.request.expected.brandName ?? "");
      setClassType(parsed.request.expected.classType ?? "");
      setAbvPercent(parsed.request.expected.abvPercent === null ? "" : String(parsed.request.expected.abvPercent));
      setProof(parsed.request.expected.proof === null ? "" : String(parsed.request.expected.proof));
      setIncludeGovernmentWarning(parsed.request.expected.governmentWarning !== null);
      setAdditionalExpectedFields(parsed.request.expected.additionalFields);
      setCaseName(parsed.displayName);
      setFile(artworkFiles[0] ?? null);
      setAdditionalFiles(artworkFiles.slice(1));
      setPreviewUrl(artworkFiles[0] ? URL.createObjectURL(artworkFiles[0]) : "");
      await enqueueCase(parsed.request, artworkFiles, parsed.displayName, {
        closeEntry: false,
        successMessage: `${parsed.displayName} was added to the queue${autoProcess ? " and started automatically" : ""}.`,
      });
    } catch (caught) {
      setError(caught instanceof SyntaxError ? "The application file is not valid JSON." : caught instanceof Error ? caught.message : "The case could not be imported.");
    } finally {
      setImportingCase(false);
    }
  };

  const validateBatchFiles = async (files: File[]) => {
    setValidatingBatch(true);
    setError("");
    setImportSummary("");
    setBatchPreview(null);
    try {
      setBatchPreview(await previewBatchImportFiles(files));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The selected files could not be checked.");
    } finally {
      setValidatingBatch(false);
    }
  };

  const importBatchFiles = async (preview: BatchImportPreview) => {
    const issues = [...preview.issues];
    const createdCases: QueueCaseDetail[] = [];
    let imported = 0;
    let duplicates = 0;
    setImportingCase(true);
    setError("");
    setImportSummary("");
    try {
      for (const pair of preview.ready) {
        try {
          // Bulk import is deliberately two-phase. Creating every item first
          // lets us start recognition in the same newest-first order shown in
          // the queue instead of letting the bottom item claim the first slot.
          const created = await createQueuedCase(
            pair.imported.request,
            pair.artwork,
            pair.imported.displayName,
            false,
          );
          createdCases.push(created);
          imported += 1;
          if (created.duplicateImageCount > 0) duplicates += 1;
        } catch (caught) {
          const message = caught instanceof SyntaxError
            ? "application file is not valid JSON"
            : caught instanceof Error ? caught.message : "could not be imported";
          issues.push(`${pair.application.name}: ${message}`);
        }
      }
      if (autoProcess) {
        for (const created of [...createdCases].reverse()) {
          const response = await apiFetch(`${API_URL}/api/v1/cases/${created.caseId}/scan`, { method: "POST" });
          if (!response.ok) {
            const payload = await response.json().catch(() => null);
            issues.push(`${created.displayName}: ${errorMessage(payload, "could not start recognition")}`);
          }
        }
      }
      await refreshQueue();
      const success = imported ? `${imported} ${imported === 1 ? "case" : "cases"} added to the queue${autoProcess ? " and started automatically" : ""}.` : "No cases were added.";
      const duplicateWarning = duplicates ? ` ${duplicates} ${duplicates === 1 ? "import uses" : "imports use"} artwork already in the queue.` : "";
      const failures = issues.length ? ` ${issues.slice(0, 8).join(" · ")}${issues.length > 8 ? ` · and ${issues.length - 8} more` : ""}` : "";
      setImportSummary(`${success}${duplicateWarning}${failures}`);
      setBatchPreview({ ready: [], issues: preview.issues });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The batch could not be imported.");
    } finally {
      setImportingCase(false);
    }
  };

  const reset = () => {
    setAnalysis(null);
    setActiveCase(null);
    setReviewNote("");
    setError("");
    setHoverEvidence([]);
    setPinnedEvidence([]);
    setPanelIndex(0);
  };

  const openQueueEntry = (mode: "single" | "batch" | "catalog") => {
    reset();
    setCaseName("");
    setIncludeGovernmentWarning(true);
    setAdditionalExpectedFields([]);
    setFile(null);
    setAdditionalFiles([]);
    setPreviewUrl("");
    setImportSummary("");
    setBatchPreview(null);
    setQueueEntryMode(mode);
    setActiveTab("queue");
  };

  const openSettings = () => {
    setReaderModeDraft(readerMode);
    void loadCacheStats();
    setActiveTab("settings");
  };

  const clearRecognitionCache = async () => {
    if (!window.confirm("Clear all cached OCR and LLM recognition results on this server? Existing case results will not be changed.")) return;
    setCacheClearing(true);
    setCacheError("");
    try {
      const response = await apiFetch(`${API_URL}/api/v1/recognition-cache`, { method: "DELETE" });
      if (!response.ok) throw new Error("The recognition cache could not be cleared.");
      setCacheStats({ schemaVersion: "recognition-cache-stats-v1", ocrEntries: 0, llmEntries: 0, totalEntries: 0 });
    } catch (caught) {
      setCacheError(caught instanceof Error ? caught.message : "The recognition cache could not be cleared.");
    } finally {
      setCacheClearing(false);
    }
  };

  const saveSettings = async () => {
    const nextMode = readerCapabilities.visionAvailable ? readerModeDraft : "ocr";
    const response = await apiFetch(`${API_URL}/api/v1/preferences/analysis`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ readerMode: nextMode }),
    });
    if (!response.ok) {
      setError("Analysis settings could not be saved.");
      return;
    }
    setReaderMode(nextMode);
    setActiveTab("queue");
  };

  const cancelSettings = () => {
    setReaderModeDraft(readerMode);
    setActiveTab("queue");
  };

  const openCase = async (caseId: string) => {
    setError("");
    setQueueEntryMode(null);
    setSubmitting(true);
    try {
      let detail = await fetchCase(caseId);
      if (needsHumanReview(detail) && !detail.assignedToUserId) {
        const claim = await apiFetch(`${API_URL}/api/v1/cases/${caseId}/claim`, { method: "POST" });
        if (!claim.ok) {
          const payload = await claim.json().catch(() => null);
          throw new Error(errorMessage(payload, `Case could not be claimed (${claim.status})`));
        }
        detail = (await claim.json()) as QueueCaseDetail;
        await refreshQueue();
      }
      setCategory(detail.category);
      setBrandName(detail.expected.brandName ?? "");
      setClassType(detail.expected.classType ?? "");
      setAbvPercent(detail.expected.abvPercent === null ? "" : String(detail.expected.abvPercent));
      setProof(detail.expected.proof === null ? "" : String(detail.expected.proof));
      setIncludeGovernmentWarning(detail.expected.governmentWarning !== null);
      setAdditionalExpectedFields(detail.expected.additionalFields ?? []);
      setFile(null);
      setAdditionalFiles([]);
      setPreviewUrl("");
      setActiveCase(detail);
      setAnalysis(detail.analysis);
      setReviewNote(detail.reviewNote ?? "");
      setHoverEvidence([]);
      setPinnedEvidence([]);
      setPanelIndex(0);
      setActiveTab("review");
    } catch (caught) {
      setQueueError(caught instanceof Error ? caught.message : "The case could not be opened.");
    } finally {
      setSubmitting(false);
    }
  };

  const openNextReview = async (completedReview = false) => {
    setFinishedReviewQueue(completedReview);
    const cases = await refreshQueue();
    const next = cases.find((item) => needsHumanReview(item) && (!item.assignedToUserId || item.assignedToUserId === currentUser?.userId));
    if (next) {
      setFinishedReviewQueue(false);
      await openCase(next.caseId);
      return;
    }
    reset();
    setFile(null);
    setAdditionalFiles([]);
    setPreviewUrl("");
    setActiveTab("review");
  };

  const scanCase = async (caseId: string) => {
    setScanningCaseId(caseId);
    setQueueError("");
    try {
      const response = await apiFetch(`${API_URL}/api/v1/cases/${caseId}/scan`, { method: "POST" });
      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        throw new Error(errorMessage(payload, `Scan could not start (${response.status})`));
      }
      await refreshQueue();
    } catch (caught) {
      setQueueError(caught instanceof Error ? caught.message : "The scan could not be started.");
    } finally {
      setScanningCaseId("");
    }
  };

  const removeCase = async (caseId: string) => {
    setRemovingCaseId(caseId);
    setQueueError("");
    try {
      const response = await apiFetch(`${API_URL}/api/v1/cases/${caseId}`, { method: "DELETE" });
      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        throw new Error(errorMessage(payload, `Case could not be removed (${response.status})`));
      }
      if (activeCase?.caseId === caseId) {
        reset();
        setActiveTab("queue");
      }
      await refreshQueue();
    } catch (caught) {
      setQueueError(caught instanceof Error ? caught.message : "The queue item could not be removed.");
    } finally {
      setRemovingCaseId("");
    }
  };

  const restoreCase = async (caseId: string) => {
    setRemovingCaseId(caseId);
    setQueueError("");
    try {
      const response = await apiFetch(`${API_URL}/api/v1/cases/${caseId}/restore`, { method: "POST" });
      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        throw new Error(errorMessage(payload, `Case could not be restored (${response.status})`));
      }
      await refreshQueue();
    } catch (caught) {
      setQueueError(caught instanceof Error ? caught.message : "The queue item could not be restored.");
    } finally {
      setRemovingCaseId("");
    }
  };

  const changeClaim = async (caseId: string, action: "claim" | "release") => {
    setClaimingCaseId(caseId);
    setQueueError("");
    try {
      const response = await apiFetch(`${API_URL}/api/v1/cases/${caseId}/${action}`, { method: "POST" });
      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        throw new Error(errorMessage(payload, `Case could not be ${action === "claim" ? "claimed" : "released"} (${response.status})`));
      }
      await refreshQueue();
    } catch (caught) {
      setQueueError(caught instanceof Error ? caught.message : "The assignment could not be updated.");
    } finally { setClaimingCaseId(""); }
  };

  const logout = async () => {
    await apiFetch(`${API_URL}/api/v1/auth/logout`, { method: "POST" }).catch(() => undefined);
    setCurrentUser(null); setAuthState("login"); setQueueCases([]); setQueueLoading(false);
  };

  const clearQueue = async () => {
    if (!window.confirm("Clear every case from this local testing queue? This permanently deletes queued artwork, including removed cases.")) return;
    setClearingQueue(true);
    setQueueError("");
    try {
      const response = await apiFetch(`${API_URL}/api/v1/cases`, { method: "DELETE" });
      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        throw new Error(errorMessage(payload, `Queue could not be cleared (${response.status})`));
      }
      reset();
      setFile(null);
      setAdditionalFiles([]);
      setPreviewUrl("");
      setActiveTab("queue");
      await refreshQueue();
    } catch (caught) {
      setQueueError(caught instanceof Error ? caught.message : "The queue could not be cleared.");
    } finally {
      setClearingQueue(false);
    }
  };

  const saveDecision = async (outcome: CaseOutcome) => {
    if (!activeCase) return;
    setSavingDecision(true);
    setError("");
    try {
      const response = await apiFetch(`${API_URL}/api/v1/cases/${activeCase.caseId}/decision`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ outcome, note: reviewNote }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        throw new Error(errorMessage(payload, `Decision could not be saved (${response.status})`));
      }
      const updated = (await response.json()) as QueueCaseDetail;
      setActiveCase(updated);
      await openNextReview(true);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The review decision could not be saved.");
    } finally {
      setSavingDecision(false);
    }
  };

  if (authState === "checking") return <main className="loading-screen"><LoaderCircle className="spin" size={28} /><h1>Label verification</h1><p>Checking your session…</p></main>;
  if (authState === "login" || !currentUser) return <LoginScreen onLogin={(username, password) => void login(username, password)} busy={loginBusy} error={loginError} conflict={sessionConflict} onReplace={() => pendingLogin && void login(pendingLogin.username, pendingLogin.password, true)} />;

  return (
    <main className="app-shell verify-shell">
      <header className="topbar verify-topbar">
        <a className="verify-console-brand" href="/" aria-label="Return to the work queue">
          <Menu size={21} />
          <span>RO</span>
          <b>Regulatory operations console</b>
        </a>
        <div className="verify-product-title" aria-label="Label Verification">
          <ShieldCheck size={21} />
          <strong>Label verification</strong>
        </div>
        <div className="topbar-user"><span><UserRound size={14} /> {currentUser.username}</span><button type="button" onClick={() => void logout()}><LogOut size={14} /> Sign out</button></div>
      </header>
      <nav className="verify-mode-tabs" aria-label="Verification workspace">
        <button className={activeTab === "queue" ? "active" : ""} type="button" onClick={() => { setActiveTab("queue"); void refreshQueue(); }}>
          <ClipboardList size={17} /> Work queue
        </button>
        <button className={activeTab === "review" ? "active" : ""} type="button" onClick={() => void openNextReview(false)}>
          <ScanText size={17} /> Review
          {reviewableCaseCount > 0 && <em>{reviewableCaseCount}</em>}
        </button>
        <button className={activeTab === "settings" ? "active" : ""} type="button" onClick={openSettings}>
          <Settings2 size={17} /> Settings
        </button>
      </nav>

      {activeTab === "queue" ? (
        <WorkQueue
          cases={queueCases}
          loading={queueLoading}
          error={queueError}
          scanningCaseId={scanningCaseId}
          removingCaseId={removingCaseId}
          clearingQueue={clearingQueue}
          onNewCase={() => openQueueEntry("single")}
          onBatchImport={() => openQueueEntry("batch")}
          onCatalogImport={() => openQueueEntry("catalog")}
          onOpenCase={(caseId) => void openCase(caseId)}
          onScanCase={(caseId) => void scanCase(caseId)}
          onRemoveCase={(caseId) => void removeCase(caseId)}
          onRestoreCase={(caseId) => void restoreCase(caseId)}
          onClearQueue={() => void clearQueue()}
          currentUser={currentUser}
          savedFilters={queuePreferences}
          onFiltersChange={saveQueuePreferences}
          claimingCaseId={claimingCaseId}
          onClaimCase={(caseId) => void changeClaim(caseId, "claim")}
          onReleaseCase={(caseId) => void changeClaim(caseId, "release")}
          entry={queueEntryMode === "catalog" ? (
            <CatalogImport
              apiUrl={API_URL}
              apiFetch={apiFetch}
              autoProcess={autoProcess}
              readerMode={readerCapabilities.visionAvailable ? readerMode : "ocr"}
              onDone={refreshQueue}
            />
          ) : queueEntryMode ? (
            <CaseEntryForm
              mode={queueEntryMode}
              category={category}
              brandName={brandName}
              classType={classType}
              abvPercent={abvPercent}
              proof={proof}
              includeGovernmentWarning={includeGovernmentWarning}
              additionalFields={additionalExpectedFields}
              caseName={caseName}
              file={file}
              additionalFiles={additionalFiles}
              autoProcess={autoProcess}
              error={error}
              submitting={submitting}
              importing={importingCase}
              validatingBatch={validatingBatch}
              batchPreview={batchPreview}
              importSummary={importSummary}
              onCategoryChange={setCategory}
              onBrandNameChange={setBrandName}
              onClassTypeChange={setClassType}
              onAbvPercentChange={setAbvPercent}
              onProofChange={setProof}
              onIncludeGovernmentWarningChange={setIncludeGovernmentWarning}
              onAdditionalFieldsChange={setAdditionalExpectedFields}
              onCaseNameChange={setCaseName}
              onAutoProcessChange={setAutoProcess}
              onSelectUploadedFiles={selectUploadedFiles}
              onRemovePanel={removePanel}
              onMovePanel={movePanel}
              onImport={(application, artwork) => void importCaseFiles(application, artwork)}
              onValidateBatch={(files) => void validateBatchFiles(files)}
              onBatchImport={(preview) => void importBatchFiles(preview)}
              onManualSubmit={() => void submit()}
            />
          ) : null}
          onCloseEntry={() => setQueueEntryMode(null)}
        />
      ) : activeTab === "settings" ? (
        <section className="verify-settings" aria-labelledby="settings-title">
          <span className="eyebrow">Analysis settings</span>
          <h1 id="settings-title">Choose the evidence readers</h1>
          <p>This selection applies to new scans. Completed cases retain the reader mode used for their analysis.</p>
          <fieldset>
            <legend>Reader mode</legend>
            {READER_CHOICES.filter(([mode]) => readerCapabilities.visionAvailable || mode === "ocr").map(([mode, label, description]) => (
              <label aria-label={label} className={`verify-reader-choice ${readerModeDraft === mode ? "selected" : ""}`} htmlFor={`reader-mode-${mode}`} key={mode}>
                <input id={`reader-mode-${mode}`} type="radio" name="reader-mode" value={mode} checked={readerModeDraft === mode} onChange={() => setReaderModeDraft(mode)} />
                <span><b>{label}</b><small>{description}</small></span>
              </label>
            ))}
          </fieldset>
          {!readerCapabilities.visionAvailable && <p className="verify-settings-unavailable">LLM vision is unavailable because this server does not have an OpenRouter key configured.</p>}
          <section className="verify-cache-settings" aria-labelledby="cache-settings-title">
            <div className="verify-cache-settings-heading">
              <Database size={19} />
              <span><b id="cache-settings-title">Recognition cache</b><small>Identical artwork can reuse results from the same reader configuration.</small></span>
            </div>
            {cacheStatsLoading ? <div className="verify-cache-loading"><LoaderCircle className="spin" size={16} /> Counting cached results…</div> : cacheStats ? (
              <div className="verify-cache-counts" aria-label={`${cacheStats.totalEntries} cached recognition results`}>
                <span><b>{cacheStats.totalEntries}</b><small>Total results</small></span>
                <span><b>{cacheStats.ocrEntries}</b><small>OCR</small></span>
                <span><b>{cacheStats.llmEntries}</b><small>LLM</small></span>
              </div>
            ) : null}
            {cacheError && <p className="verify-cache-error"><CircleAlert size={14} /> {cacheError}</p>}
            <button className="verify-clear-cache" type="button" disabled={cacheClearing || !cacheStats?.totalEntries} onClick={() => void clearRecognitionCache()}>
              {cacheClearing ? <LoaderCircle className="spin" size={15} /> : <Trash2 size={15} />} {cacheClearing ? "Clearing cache…" : "Clear cache"}
            </button>
          </section>
          <div className="verify-settings-actions">
            <button className="secondary" type="button" onClick={cancelSettings}>Cancel</button>
            <button type="button" onClick={() => void saveSettings()} disabled={readerModeDraft === readerMode}>Save settings</button>
          </div>
        </section>
      ) : (
      <div className={`verify-body ${analysis ? "" : "no-analysis"}`}>
        {analysis && (
          <aside className="verify-intake has-result">
            <div className="verify-side-summary">
              <div className="verify-side-heading">
                <h1>Application</h1>
                <button onClick={() => { setActiveTab("queue"); void refreshQueue(); }} type="button" aria-label="Return to work queue">«</button>
              </div>
              <div className="verify-application-values">
                {activeCase && (
                  <div className="verify-property-row"><ClipboardList size={19} /><span><b>Case</b><small>{activeCase.displayName}</small></span></div>
                )}
                <div className="verify-property-row"><Wine size={19} /><span><b>Beverage category</b><small>{categoryLabel(category)}</small></span></div>
                <div className="verify-property-row"><Tag size={19} /><span><b>Expected brand name</b><small>{displayValue(brandName)}</small></span></div>
                <div className="verify-property-row"><Layers3 size={19} /><span><b>Expected class / type</b><small>{displayValue(classType)}</small></span></div>
                <div className="verify-property-row"><Percent size={19} /><span><b>Expected ABV %</b><small>{abvPercent || "—"}</small></span></div>
                <div className="verify-property-row"><ShieldCheck size={19} /><span><b>Expected proof</b><small>{category === "distilled_spirits" && proof ? proof : "—"}</small></span></div>
                {includeGovernmentWarning && <div className="verify-property-row"><ShieldCheck size={19} /><span><b>Government warning</b><small>Required statutory text</small></span></div>}
                {additionalExpectedFields.map((field) => (
                  <div className="verify-property-row" key={field.id}><ClipboardList size={19} /><span><b>{field.label}</b><small>{field.expectedText}</small></span></div>
                ))}
              </div>
              <section className="verify-side-analysis" aria-label="Analysis details">
                <h2><ScanText size={19} /> Analysis</h2>
                <div className="verify-analysis-copy">
                  {analysis.readerMode !== "ocr" && <span><b>Vision engine</b><small>LLM vision · {modelLabel(analysis.visionRun.requestedModel)}</small></span>}
                  <span><b>Analysis mode</b><small>Connected vertical slice</small></span>
                  {analysis.readerMode !== "llm" && <span><b>Provider</b><small>Tesseract OCR (local)</small></span>}
                  <span className="verify-online"><CircleDot size={11} /> Online</span>
                </div>
              </section>
            </div>
          </aside>
        )}

        <section className="verify-main">
          {!analysis ? (
            <div className="verify-welcome">
              {activeCase?.processingStatus === "processing" ? (
                <>
                  <LoaderCircle className="spin" size={38} />
                  <span className="eyebrow">Analysis in progress</span>
                  <h2>Reading {activeCase.displayName}.</h2>
                  <p>The selected reader is analyzing the artwork. This screen will update when the case is routed.</p>
                </>
              ) : (
                <>
                  <ScanText size={38} />
                  <span className="eyebrow">Review workspace</span>
                  <h2>{finishedReviewQueue ? "No more cases to review" : "No cases to review"}</h2>
                  <p>{finishedReviewQueue ? "You’ve completed every case currently routed for review." : "Machine-routed review cases will appear here when they are ready."}</p>
                  <button className="verify-welcome-action" type="button" onClick={() => setActiveTab("queue")}>Back to work queue</button>
                </>
              )}
            </div>
          ) : (
            <div className="verify-workbench">
              <EvidenceViewer
                panels={evidencePanels}
                evidence={evidenceRegions}
                activeEvidenceIds={activeEvidenceIds}
                pinnedEvidenceIds={pinnedEvidence}
                panelIndex={panelIndex}
                onPanelIndexChange={setPanelIndex}
                onClearEvidence={() => {
                  setHoverEvidence([]);
                  setPinnedEvidence([]);
                }}
                resetKey={analysis.analysisId}
                ariaLabel="Uploaded label with OCR-backed evidence"
                imageAlt={() => `Uploaded label ${file?.name ?? "panel"}`}
                toolbarTitle="Artwork"
                minimalChrome
              />

              <section className="verify-results" aria-label="Expected and detected field comparisons">
                <div className="verify-results-heading">
                  <b>Verification results</b>
                </div>
                <div className={`verify-review-banner ${analysis.overallSummary}`}>
                  <span className="verify-review-banner-icon">
                    {resultCounts.discrepancies > 0 || resultCounts.review > 0 ? <CircleAlert size={27} /> : <CheckCircle2 size={27} />}
                  </span>
                  <strong>
                    {resultCounts.discrepancies > 0
                      ? `${resultCounts.discrepancies} ${resultCounts.discrepancies === 1 ? "discrepancy needs" : "discrepancies need"} review`
                      : resultCounts.review > 0
                        ? `${resultCounts.review} ${resultCounts.review === 1 ? "field needs" : "fields need"} review`
                        : "No discrepancies need review"}
                  </strong>
                  <span className="verify-heading-counts" aria-label="Result counts">
                    <span className="verify-result-count match"><b>{resultCounts.matches}</b> match</span>
                    <span className="verify-result-count mismatch"><b>{resultCounts.discrepancies}</b> mismatch</span>
                    <span className="verify-result-count review"><b>{resultCounts.review}</b> review</span>
                  </span>
                </div>
                {activeCase && (
                  <div className="verify-decision-bar" aria-label="Human review disposition">
                    <span className="verify-current-decision">
                      {activeCase.decisionSource?.startsWith("human_") ? <UserCheck size={19} /> : <Bot size={19} />}
                      <span>
                        <b>{activeCase.decisionSource?.startsWith("human_") ? "Human reviewed" : "Automated decision"}</b>
                        <small>
                          Current outcome: {activeCase.outcome?.replace("needs_review", "needs review") ?? "pending"}
                          {activeCase.decisionSource === "human_confirmed" ? " · confirmed" : activeCase.decisionSource === "human_overridden" ? " · overridden" : ""}
                        </small>
                      </span>
                    </span>
                    <input
                      value={reviewNote}
                      onChange={(event) => setReviewNote(event.target.value)}
                      maxLength={2000}
                      placeholder={activeClaimedByAnotherReviewer ? `Assigned to ${activeCase.assignedToUsername}` : "Optional reviewer note"}
                      aria-label="Reviewer note"
                      disabled={activeClaimedByAnotherReviewer}
                    />
                    <span className="verify-decision-actions">
                      <button
                        className={`${activeCase.decisionSource?.startsWith("human_") && activeCase.outcome === "pass" ? "selected " : ""}${activeCase.automatedOutcome === "fail" ? "fail" : "pass"}`}
                        type="button"
                        disabled={savingDecision || activeClaimedByAnotherReviewer}
                        onClick={() => void saveDecision("pass")}
                      >
                        {activeCase.automatedOutcome === "fail" ? <ThumbsDown size={16} /> : <ThumbsUp size={16} />} {activeCase.automatedOutcome === "fail" ? "Reject failure" : activeCase.automatedOutcome === "pass" ? "Accept pass" : "Mark pass"}
                      </button>
                      <button
                        className={`${activeCase.decisionSource?.startsWith("human_") && activeCase.outcome === "fail" ? "selected " : ""}${activeCase.automatedOutcome === "pass" ? "fail" : activeCase.automatedOutcome === "fail" ? "pass" : "fail"}`}
                        type="button"
                        disabled={savingDecision || activeClaimedByAnotherReviewer}
                        onClick={() => void saveDecision("fail")}
                      >
                        {activeCase.automatedOutcome === "pass" ? <ThumbsDown size={16} /> : activeCase.automatedOutcome === "fail" ? <ThumbsUp size={16} /> : <ThumbsDown size={16} />} {activeCase.automatedOutcome === "pass" ? "Reject pass" : activeCase.automatedOutcome === "fail" ? "Accept failure" : "Mark failure"}
                      </button>
                    </span>
                  </div>
                )}
                {error && <div className="verify-result-error"><AlertTriangle size={15} /> {error}</div>}
                <div className="verify-results-table" role="table" aria-label="Verification results">
                  <div className="verify-results-columns" role="row">
                    <span role="columnheader">Field</span>
                    <span role="columnheader">Expected</span>
                    <span role="columnheader">Detected</span>
                    <span role="columnheader">Status</span>
                    <span role="columnheader">Evidence</span>
                  </div>
                  <div className="verify-results-scroll">
                    {RESULT_GROUPS.map((group) => {
                      const rules = visibleRuleResults.filter((rule) => group.fields.includes(rule.fieldKey));
                      if (!rules.length) return null;
                      return (
                        <section className="verify-rule-group" key={group.label} role="rowgroup">
                          <h2>
                            {group.icon === "identity" ? <Tag size={17} /> : group.icon === "alcohol" ? <FlaskConical size={17} /> : <ShieldCheck size={17} />}
                            {group.label}
                          </h2>
                          {rules.map((rule) => {
                            const visionIds = inspectableVisionIds(rule);
                            const ocrIds = inspectableOcrIds(rule);
                            return (
                              <div
                                key={rule.ruleId}
                                className={`verify-rule-row field-${rule.fieldKey} ${rule.automatedStatus}`}
                                role="row"
                              >
                                <span className="verify-rule-field" role="cell" title={rule.explanation}>{FIELD_LABELS[rule.fieldKey]}</span>
                                <ResultGridValue fieldKey={rule.fieldKey} value={rule.expectedValue} />
                                <ResultGridValue fieldKey={rule.fieldKey} value={rule.detectedValue} detected />
                                <span className="verify-rule-status" role="cell" title={rule.explanation}>
                                  {rule.automatedStatus === "matches" ? <CheckCircle2 size={20} /> : rule.automatedStatus === "discrepancy" ? <XCircle size={20} /> : <AlertTriangle size={20} />}
                                  <span>
                                    <b>{statusLabel(rule.automatedStatus)}</b>
                                    {rule.automatedStatus === "review" && <small>{rule.explanation}</small>}
                                  </span>
                                </span>
                                <span className="verify-evidence-cell" role="cell">
                                  {([
                                    { source: "vision", label: "LLM", name: "Gemini", ids: visionIds },
                                    { source: "ocr", label: "OCR", name: "Tesseract", ids: ocrIds },
                                  ] as const).filter((evidence) => (
                                    evidence.source === "vision" ? analysis.readerMode !== "ocr" : analysis.readerMode !== "llm"
                                  )).map((evidence) => {
                                    const located = evidence.ids.length > 0;
                                    const pinned = evidence.ids.some((id) => pinnedEvidence.includes(id));
                                    return (
                                      <button
                                        className={`source-${evidence.source}`}
                                        key={evidence.source}
                                        type="button"
                                        disabled={!located}
                                        aria-pressed={pinned}
                                        aria-label={located ? `Inspect ${evidence.name} evidence for ${FIELD_LABELS[rule.fieldKey]}` : `No ${evidence.name} evidence located for ${FIELD_LABELS[rule.fieldKey]}`}
                                        title={located ? `${evidence.name} box · hover to zoom, click to pin` : `${evidence.name} could not locate this text on the artwork`}
                                        onMouseEnter={() => located && showHoveredEvidence(evidence.ids)}
                                        onMouseLeave={clearHoveredEvidence}
                                        onFocus={() => located && showHoveredEvidence(evidence.ids)}
                                        onBlur={clearHoveredEvidence}
                                        onClick={() => {
                                          if (!located) return;
                                          setHoverEvidence([]);
                                          setPinnedEvidence(pinned ? [] : evidence.ids);
                                        }}
                                      >
                                        <Eye size={18} />
                                        <small>{evidence.label}</small>
                                      </button>
                                    );
                                  })}
                                </span>
                              </div>
                            );
                          })}
                        </section>
                      );
                    })}
                    {visibleAdditionalRuleResults.length > 0 && (
                      <section className="verify-rule-group" role="rowgroup">
                        <h2><ClipboardList size={17} />Additional expected text</h2>
                        {visibleAdditionalRuleResults.map((rule) => (
                          <div className={`verify-rule-row additional-field ${rule.automatedStatus}`} key={rule.fieldId} role="row">
                            <span className="verify-rule-field" role="cell" title={rule.explanation}>{rule.label}</span>
                            <span className="verify-rule-value" role="cell" title={rule.expectedValue}>{displayValue(rule.expectedValue)}</span>
                            <span className="verify-rule-value detected" role="cell" title={rule.detectedValue ?? ""}>{displayValue(rule.detectedValue)}</span>
                            <span className="verify-rule-status" role="cell" title={rule.explanation}>
                              {rule.automatedStatus === "matches" ? <CheckCircle2 size={20} /> : rule.automatedStatus === "discrepancy" ? <XCircle size={20} /> : <AlertTriangle size={20} />}
                              <span><b>{statusLabel(rule.automatedStatus)}</b>{rule.automatedStatus === "review" && <small>{rule.explanation}</small>}</span>
                            </span>
                            <span className="verify-evidence-cell verify-transcript-evidence" role="cell" title={rule.evidenceQuote ?? "The expected phrase was not found in the reader transcript."}>
                              {rule.evidenceQuote ? "Transcript" : "—"}
                            </span>
                          </div>
                        ))}
                      </section>
                    )}
                  </div>
                </div>
              </section>
            </div>
          )}
        </section>
      </div>
      )}
    </main>
  );
}
