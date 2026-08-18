import {
  Bot,
  CheckCircle2,
  CircleAlert,
  Clock3,
  Copy,
  Eye,
  PanelLeftClose,
  FilePlus2,
  CloudDownload,
  LoaderCircle,
  Play,
  RotateCw,
  Search,
  Trash2,
  UserCheck,
  UserPlus,
  UserRound,
  XCircle,
} from "lucide-react";
import type { ReactNode } from "react";
import { useEffect, useMemo, useState } from "react";
import type {
  CaseOutcome,
  DecisionSource,
  QueueCase,
  QueuePreferences,
  QueueUser,
} from "../verification-types";

type QueueProps = {
  cases: QueueCase[];
  loading: boolean;
  error: string;
  scanningCaseId: string;
  removingCaseId: string;
  clearingQueue: boolean;
  onNewCase: () => void;
  onBatchImport: () => void;
  onCatalogImport: () => void;
  onOpenCase: (caseId: string) => void;
  onScanCase: (caseId: string) => void;
  onRemoveCase: (caseId: string) => void;
  onRestoreCase: (caseId: string) => void;
  onClearQueue: () => void;
  entry: ReactNode | null;
  onCloseEntry: () => void;
  currentUser: QueueUser;
  savedFilters?: QueuePreferences | null;
  onFiltersChange: (filters: QueuePreferences) => void;
  claimingCaseId: string;
  onClaimCase: (caseId: string) => void;
  onReleaseCase: (caseId: string) => void;
};

const OUTCOME_LABELS: Record<CaseOutcome, string> = {
  pass: "Passed",
  fail: "Failed",
  needs_review: "Needs review",
};

const QUEUE_FILTERS_STORAGE_KEY = "ttb-label-work-queue-filters-v1";
type OutcomeFilter = "all" | CaseOutcome;
type ReviewFilter = "all" | "attention" | "automated" | "human";

type QueueFilters = QueuePreferences;

const DEFAULT_QUEUE_FILTERS: QueueFilters = {
  query: "",
  outcomeFilter: "all",
  reviewFilter: "all",
  assignmentFilter: "all",
  showRemoved: false,
};

function savedQueueFilters(): QueueFilters {
  try {
    const raw = window.localStorage.getItem(QUEUE_FILTERS_STORAGE_KEY);
    if (!raw) return DEFAULT_QUEUE_FILTERS;
    const saved: unknown = JSON.parse(raw);
    if (!saved || typeof saved !== "object") return DEFAULT_QUEUE_FILTERS;
    const value = saved as Partial<QueueFilters>;
    return {
      query: typeof value.query === "string" ? value.query : "",
      outcomeFilter: value.outcomeFilter === "pass" || value.outcomeFilter === "fail" || value.outcomeFilter === "needs_review" ? value.outcomeFilter : "all",
      reviewFilter: value.reviewFilter === "attention" || value.reviewFilter === "automated" || value.reviewFilter === "human" ? value.reviewFilter : "all",
      assignmentFilter: value.assignmentFilter === "mine" || value.assignmentFilter === "unassigned" ? value.assignmentFilter : "all",
      showRemoved: value.showRemoved === true,
    };
  } catch {
    return DEFAULT_QUEUE_FILTERS;
  }
}

function decisionLabel(source: DecisionSource | null) {
  if (source === "human_confirmed") return "Human confirmed";
  if (source === "human_overridden") return "Human override";
  if (source === "automated") return "Automated";
  return "Pending";
}

function workflowLabel(item: QueueCase) {
  return item.outcome ? OUTCOME_LABELS[item.outcome] : "—";
}

function categoryLabel(category: QueueCase["category"]) {
  if (category === "distilled_spirits") return "Distilled spirits";
  if (category === "malt_beverage") return "Malt beverage";
  return "Wine";
}

function formatTime(value: string) {
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(value));
}

export default function WorkQueue({
  cases,
  loading,
  error,
  scanningCaseId,
  removingCaseId,
  clearingQueue,
  onNewCase,
  onBatchImport,
  onCatalogImport,
  onOpenCase,
  onScanCase,
  onRemoveCase,
  onRestoreCase,
  onClearQueue,
  entry,
  onCloseEntry,
  currentUser,
  savedFilters,
  onFiltersChange,
  claimingCaseId,
  onClaimCase,
  onReleaseCase,
}: QueueProps) {
  const [initialFilters] = useState(() => savedFilters ?? savedQueueFilters());
  const [query, setQuery] = useState(initialFilters.query);
  const [outcomeFilter, setOutcomeFilter] = useState<OutcomeFilter>(initialFilters.outcomeFilter);
  const [reviewFilter, setReviewFilter] = useState<ReviewFilter>(initialFilters.reviewFilter);
  const [assignmentFilter, setAssignmentFilter] = useState<QueueFilters["assignmentFilter"]>(initialFilters.assignmentFilter);
  const [showRemoved, setShowRemoved] = useState(initialFilters.showRemoved);

  // Server preferences arrive just after the authenticated workspace mounts.
  // Keep localStorage as a useful fallback, then let the user's account win.
  /* eslint-disable react-hooks/set-state-in-effect -- account preferences intentionally hydrate local filter controls */
  useEffect(() => {
    if (!savedFilters) return;
    setQuery(savedFilters.query);
    setOutcomeFilter(savedFilters.outcomeFilter);
    setReviewFilter(savedFilters.reviewFilter);
    setAssignmentFilter(savedFilters.assignmentFilter);
    setShowRemoved(savedFilters.showRemoved);
  }, [savedFilters]);
  /* eslint-enable react-hooks/set-state-in-effect */

  useEffect(() => {
    window.localStorage.setItem(
      QUEUE_FILTERS_STORAGE_KEY,
      JSON.stringify({ query, outcomeFilter, reviewFilter, assignmentFilter, showRemoved }),
    );
    onFiltersChange({ query, outcomeFilter, reviewFilter, assignmentFilter, showRemoved });
  }, [assignmentFilter, outcomeFilter, onFiltersChange, query, reviewFilter, showRemoved]);

  const filtered = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase();
    return cases.filter((item) => {
      if (!showRemoved && item.removedAt) return false;
      if (outcomeFilter !== "all" && item.outcome !== outcomeFilter && item.decisionStatus !== outcomeFilter) return false;
      if (reviewFilter === "attention" && !(item.outcome === "needs_review" && item.decisionSource === "automated")) return false;
      if (reviewFilter === "automated" && item.decisionSource !== "automated") return false;
      if (reviewFilter === "human" && !item.decisionSource?.startsWith("human_")) return false;
      if (assignmentFilter === "mine" && item.assignedToUserId !== currentUser.userId) return false;
      if (assignmentFilter === "unassigned" && item.assignedToUserId) return false;
      if (!normalizedQuery) return true;
      return [
        item.displayName,
        item.expected.brandName,
        item.expected.classType,
        item.caseId,
        ...(item.expected.additionalFields ?? []).flatMap((field) => [field.label, field.expectedText]),
      ]
        .filter((value): value is string => Boolean(value))
        .some((value) => value.toLocaleLowerCase().includes(normalizedQuery));
    });
  }, [assignmentFilter, cases, currentUser.userId, outcomeFilter, query, reviewFilter, showRemoved]);

  const counts = {
    pass: cases.filter((item) => !item.removedAt && item.outcome === "pass").length,
    fail: cases.filter((item) => !item.removedAt && item.outcome === "fail").length,
    review: cases.filter((item) => !item.removedAt && item.outcome === "needs_review").length,
    processing: cases.filter((item) => !item.removedAt && (item.processingStatus === "processing" || item.processingStatus === "queued")).length,
    removed: cases.filter((item) => item.removedAt).length,
  };

  return (
    <section className="queue-page" aria-label="Label verification work queue">
      <header className="queue-hero">
        <div>
          <span className="eyebrow">Review operations</span>
          <h1>Work queue</h1>
          <p>Automated comparison keeps clear decisions moving and sends uncertain evidence to a reviewer.</p>
        </div>
        <div className="queue-hero-actions">
          <button className="queue-clear-button" type="button" disabled={clearingQueue || cases.length === 0} onClick={onClearQueue}>
            {clearingQueue ? <LoaderCircle className="spin" size={16} /> : <Trash2 size={16} />} Clear queue
          </button>
          <button className="queue-batch-button" type="button" onClick={onBatchImport}>
            <FilePlus2 size={17} /> Batch import
          </button>
          <button className="queue-catalog-button" type="button" onClick={onCatalogImport}>
            <CloudDownload size={17} /> Import from catalog
          </button>
          <button className="queue-new-button" type="button" onClick={onNewCase}>
            <FilePlus2 size={18} /> Add case
          </button>
        </div>
      </header>

      <div className="queue-stat-grid" aria-label="Queue summary">
        <article><CheckCircle2 /><span><b>{counts.pass}</b><small>Passed</small></span></article>
        <article><XCircle /><span><b>{counts.fail}</b><small>Failed</small></span></article>
        <article><CircleAlert /><span><b>{counts.review}</b><small>Needs review</small></span></article>
        <article><Clock3 /><span><b>{counts.processing}</b><small>In progress</small></span></article>
        <article className="queue-removed-stat"><Trash2 /><span><b>{counts.removed}</b><small>Removed</small></span></article>
      </div>

      <div className="queue-toolbar">
        <label className="queue-search">
          <Search size={17} />
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search applications" />
        </label>
        <label>
          <span>Outcome</span>
          <select value={outcomeFilter} onChange={(event) => setOutcomeFilter(event.target.value as typeof outcomeFilter)}>
            <option value="all">All outcomes</option>
            <option value="pass">Passed</option>
            <option value="fail">Failed</option>
            <option value="needs_review">Needs review</option>
          </select>
        </label>
        <label>
          <span>Assignment</span>
          <select value={assignmentFilter} onChange={(event) => setAssignmentFilter(event.target.value as typeof assignmentFilter)}>
            <option value="all">All cases</option>
            <option value="mine">Assigned to me</option>
            <option value="unassigned">Unassigned</option>
          </select>
        </label>
        <label>
          <span>Review status</span>
          <select value={reviewFilter} onChange={(event) => setReviewFilter(event.target.value as typeof reviewFilter)}>
            <option value="all">All decisions</option>
            <option value="attention">Human review required</option>
            <option value="automated">Automated</option>
            <option value="human">Human reviewed</option>
          </select>
        </label>
        <label className="queue-show-removed">
          <input type="checkbox" checked={showRemoved} onChange={(event) => setShowRemoved(event.target.checked)} />
          <span>Show removed</span>
        </label>
      </div>

      {error && <div className="queue-error"><CircleAlert size={17} /> {error}</div>}

      <div className="queue-table" role="table" aria-label="Cases">
        <div className="queue-table-head" role="row">
          <span>Application</span><span>Category</span><span>Assignment</span><span>Processing</span><span>Outcome</span><span>Decision</span><span>Updated</span><span />
        </div>
        <div className="queue-table-body">
          {loading && cases.length === 0 ? (
            <div className="queue-empty"><LoaderCircle className="spin" size={24} /> Loading queue…</div>
          ) : filtered.length === 0 ? (
            <div className="queue-empty">
              <FilePlus2 size={28} />
              <b>{cases.length ? "No cases match these filters" : "Your work queue is empty"}</b>
              <span>{cases.length ? "Change the filters to see more cases." : "Add an application and its label artwork to get started."}</span>
              {!cases.length && (
                <div className="queue-empty-actions">
                  <button type="button" onClick={onNewCase}>Add case</button>
                  <button type="button" onClick={onBatchImport}>Batch import</button>
                  <button type="button" onClick={onCatalogImport}>Import from catalog</button>
                </div>
              )}
            </div>
          ) : filtered.map((item) => {
            const mayScan = !item.removedAt && (item.processingStatus === "queued" || item.processingStatus === "error");
            const processing = item.processingStatus === "processing" || scanningCaseId === item.caseId;
            const removing = removingCaseId === item.caseId;
            return (
              <div className={`queue-row ${item.removedAt ? "is-removed" : ""}`} role="row" key={item.caseId}>
                <span className="queue-case-name">
                  <b>{item.displayName}</b>
                  <small>{item.expected.brandName ?? "Expected brand not provided"} · {item.panel.file}{item.panels.length > 1 ? ` · ${item.panels.length} panels` : ""}</small>
                  {item.duplicateImageCount > 0 && <em className="queue-duplicate-tag" title={`Same artwork as ${item.duplicateImageCount} other queue ${item.duplicateImageCount === 1 ? "item" : "items"}`}><Copy size={11} /> Duplicate artwork</em>}
                </span>
                <span>{categoryLabel(item.category)}</span>
                <span className="queue-assignment">
                  {item.assignedToUsername ? <><UserRound size={14} /><span>{item.assignedToUsername}</span></> : <span>Unassigned</span>}
                  {item.createdByUsername && <small>Added by {item.createdByUsername}</small>}
                </span>
                <span className={`queue-status processing-${item.processingStatus}`}>
                  {processing ? <LoaderCircle className="spin" size={15} /> : item.processingStatus === "error" ? <CircleAlert size={15} /> : item.processingStatus === "complete" ? <CheckCircle2 size={15} /> : <Clock3 size={15} />}
                  {processing ? "Processing" : item.processingStatus}
                </span>
                <span><em className={`queue-outcome ${item.outcome ?? item.decisionStatus}`}>{workflowLabel(item)}</em></span>
                <span className="queue-decision-source">
                  {item.decisionSource?.startsWith("human_") ? <UserCheck size={15} /> : <Bot size={15} />}
                  {decisionLabel(item.decisionSource)}
                </span>
                <span>{formatTime(item.updatedAt)}</span>
                <span className="queue-actions">
                  {mayScan && (
                    <button type="button" disabled={processing} onClick={() => onScanCase(item.caseId)}>
                      {item.processingStatus === "error" ? <RotateCw size={15} /> : <Play size={15} />}
                      {item.processingStatus === "error" ? "Retry" : "Scan"}
                    </button>
                  )}
                  {!item.removedAt && item.processingStatus === "complete" && (
                    <button type="button" onClick={() => onOpenCase(item.caseId)}><Eye size={15} /> Review</button>
                  )}
                  {!item.removedAt && (
                    item.assignedToUserId === currentUser.userId ? (
                      <button type="button" disabled={claimingCaseId === item.caseId} onClick={() => onReleaseCase(item.caseId)}><UserCheck size={15} /> Release</button>
                    ) : !item.assignedToUserId ? (
                      <button type="button" disabled={claimingCaseId === item.caseId} onClick={() => onClaimCase(item.caseId)}><UserPlus size={15} /> Claim</button>
                    ) : null
                  )}
                  {item.removedAt ? (
                    <button className="queue-restore-button" type="button" disabled={removing} onClick={() => onRestoreCase(item.caseId)}>{removing ? <LoaderCircle className="spin" size={15} /> : <RotateCw size={15} />} Put back</button>
                  ) : (
                    <button className="queue-remove-button" type="button" disabled={removing} onClick={() => onRemoveCase(item.caseId)}><Trash2 size={15} /> Remove</button>
                  )}
                </span>
              </div>
            );
          })}
        </div>
      </div>
      {entry && (
        <div className="queue-entry-overlay" role="dialog" aria-modal="true" aria-label="Add a queue case">
          <button className="queue-entry-backdrop" type="button" onClick={onCloseEntry} aria-label="Close case entry" />
          <aside className="verify-intake queue-entry-drawer">
            <button className="queue-entry-close" type="button" onClick={onCloseEntry}><PanelLeftClose size={17} /> Close</button>
            {entry}
          </aside>
        </div>
      )}
    </section>
  );
}
