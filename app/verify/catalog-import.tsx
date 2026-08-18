import { CheckCircle2, CircleAlert, CloudDownload, LoaderCircle, XCircle } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import type { BeverageCategory, ReaderMode, SourceCatalogCase, SourceCatalogImportJob, SourceImportResult } from "../verification-types";

type CatalogImportProps = {
  apiUrl: string;
  apiFetch: (input: string, init?: RequestInit) => Promise<Response>;
  autoProcess: boolean;
  readerMode: ReaderMode;
  onDone: () => Promise<unknown> | void;
};

type CatalogStatus = "loading" | "ready" | "unavailable";
type CatalogSourceFilter = "official" | "synthetic" | "all";
type ImportSummary = { imported: number; skipped: number; errors: number; results: SourceImportResult[] };
const ACTIVE_JOB_KEY = "treasury.activeCatalogImportJob";

function isOfficial(entry: SourceCatalogCase) {
  return entry.sourceCaseId.startsWith("official-ttb-");
}

function categoryLabel(category: BeverageCategory | null) {
  if (category === "distilled_spirits") return "Distilled spirits";
  if (category === "malt_beverage") return "Malt beverage";
  if (category === "wine") return "Wine";
  return "All categories";
}

function categoryFrom(value: unknown): BeverageCategory | null {
  return value === "wine" || value === "distilled_spirits" || value === "malt_beverage" ? value : null;
}

/** Accept the documented API response and a couple of harmless envelope variations. */
function catalogCases(payload: unknown): SourceCatalogCase[] {
  if (!payload || typeof payload !== "object") return [];
  const root = payload as Record<string, unknown>;
  const candidates = Array.isArray(root.cases)
    ? root.cases
    : root.catalog && typeof root.catalog === "object" && Array.isArray((root.catalog as { cases?: unknown }).cases)
      ? (root.catalog as { cases: unknown[] }).cases
      : Array.isArray(root.entries) ? root.entries : [];
  return candidates.flatMap((item): SourceCatalogCase[] => {
    if (!item || typeof item !== "object") return [];
    const source = item as Record<string, unknown>;
    const sourceCaseId = typeof source.sourceCaseId === "string" ? source.sourceCaseId
      : typeof source.source_case_id === "string" ? source.source_case_id
        : typeof source.caseId === "string" ? source.caseId : "";
    if (!sourceCaseId) return [];
    const expected = source.expected && typeof source.expected === "object" ? source.expected as SourceCatalogCase["expected"] : undefined;
    const panels = Array.isArray(source.panels) ? source.panels.length : typeof source.panelCount === "number" ? source.panelCount : 0;
    return [{
      sourceCaseId,
      displayName: typeof source.displayName === "string" ? source.displayName : sourceCaseId,
      category: categoryFrom(source.category),
      panelCount: panels,
      alreadyImported: source.alreadyImported === true || source.imported === true || typeof source.alreadyImportedCaseId === "string",
      expected,
      description: typeof source.description === "string" ? source.description : undefined,
    }];
  });
}

function errorDetail(payload: unknown, fallback: string) {
  if (!payload || typeof payload !== "object" || !("detail" in payload)) return fallback;
  const detail = (payload as { detail: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && "message" in detail && typeof (detail as { message: unknown }).message === "string") {
    return (detail as { message: string }).message;
  }
  return fallback;
}

function importJob(payload: unknown): SourceCatalogImportJob | null {
  if (!payload || typeof payload !== "object") return null;
  const value = payload as Record<string, unknown>;
  if (typeof value.jobId !== "string" || !["queued", "running", "complete", "failed"].includes(String(value.status))) return null;
  return value as unknown as SourceCatalogImportJob;
}

function jobSummary(job: SourceCatalogImportJob): ImportSummary {
  return {
    imported: job.importedCaseIds.length,
    skipped: job.duplicateCaseIds.length,
    errors: job.issues.length,
    results: job.issues.map((issue) => ({
      sourceCaseId: issue.sourceCaseId,
      status: "error",
      message: issue.message,
    })),
  };
}

export default function CatalogImport({ apiUrl, apiFetch, autoProcess, readerMode, onDone }: CatalogImportProps) {
  const [status, setStatus] = useState<CatalogStatus>("loading");
  const [catalog, setCatalog] = useState<SourceCatalogCase[]>([]);
  const [error, setError] = useState("");
  const [sourceFilter, setSourceFilter] = useState<CatalogSourceFilter>("official");
  const [category, setCategory] = useState<BeverageCategory | "all">("all");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [importing, setImporting] = useState(false);
  const [summary, setSummary] = useState<ImportSummary | null>(null);
  const [job, setJob] = useState<SourceCatalogImportJob | null>(null);

  const load = useCallback(async () => {
    setStatus("loading"); setError("");
    try {
      const response = await apiFetch(`${apiUrl}/api/v1/sources/s3/catalog`);
      const payload = await response.json().catch(() => null);
      if (!response.ok) throw new Error(errorDetail(payload, response.status === 404 ? "A shared catalog is not configured for this workspace." : "The shared catalog could not be loaded."));
      const entries = catalogCases(payload);
      setCatalog(entries);
      setSourceFilter(entries.some(isOfficial) ? "official" : "all");
      setSelected(new Set());
      setStatus("ready");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The shared catalog could not be loaded.");
      setStatus("unavailable");
    }
  }, [apiFetch, apiUrl]);

  useEffect(() => {
    // The drawer is mounted on demand; initialize its external catalog and any
    // persisted server-side job exactly once for this mount.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
    const savedJobId = window.localStorage.getItem(ACTIVE_JOB_KEY);
    if (!savedJobId) return;
    setImporting(true);
    void apiFetch(`${apiUrl}/api/v1/sources/s3/import-jobs/${savedJobId}`).then(async (response) => {
      const payload = await response.json().catch(() => null);
      const restored = response.ok ? importJob(payload) : null;
      if (!restored) {
        window.localStorage.removeItem(ACTIVE_JOB_KEY);
        setImporting(false);
        return;
      }
      setJob(restored);
      if (restored.status === "complete" || restored.status === "failed") {
        setSummary(jobSummary(restored));
        setImporting(false);
        window.localStorage.removeItem(ACTIVE_JOB_KEY);
        void onDone();
        void load();
      }
    });
  }, [apiFetch, apiUrl, load, onDone]); // The drawer is mounted afresh each time it opens.

  useEffect(() => {
    if (!job || (job.status !== "queued" && job.status !== "running")) return;
    const timer = window.setTimeout(() => {
      void apiFetch(`${apiUrl}/api/v1/sources/s3/import-jobs/${job.jobId}`).then(async (response) => {
        const payload = await response.json().catch(() => null);
        const nextJob = response.ok ? importJob(payload) : null;
        if (!nextJob) {
          setError(errorDetail(payload, "Import progress could not be loaded."));
          // Keep the job active and retry; a transient polling error does not
          // mean the server stopped processing the import.
          setJob((current) => current ? { ...current } : current);
          return;
        }
        setJob(nextJob);
        if (nextJob.status === "complete" || nextJob.status === "failed") {
          setSummary(jobSummary(nextJob));
          setImporting(false);
          window.localStorage.removeItem(ACTIVE_JOB_KEY);
          await onDone();
          await load();
        }
      }).catch(() => {
        setError("Import progress could not be loaded. The server may still be processing the import.");
        setJob((current) => current ? { ...current } : current);
      });
    }, 650);
    return () => window.clearTimeout(timer);
  }, [job, apiUrl, apiFetch, load, onDone]);

  const filtered = useMemo(() => {
    return catalog.filter((entry) => {
      const sourceMatches = sourceFilter === "all"
        || (sourceFilter === "official" ? isOfficial(entry) : !isOfficial(entry));
      return sourceMatches && (category === "all" || entry.category === category);
    });
  }, [catalog, category, sourceFilter]);

  const officialCount = catalog.filter(isOfficial).length;
  const syntheticCount = catalog.length - officialCount;

  const selectable = filtered.filter((entry) => !entry.alreadyImported);
  const allFilteredSelected = selectable.length > 0 && selectable.every((entry) => selected.has(entry.sourceCaseId));
  const toggleAll = () => setSelected((current) => {
    const next = new Set(current);
    if (allFilteredSelected) selectable.forEach((entry) => next.delete(entry.sourceCaseId));
    else {
      next.clear();
      selectable.forEach((entry) => next.add(entry.sourceCaseId));
    }
    return next;
  });

  const submit = async () => {
    const requested = catalog.filter((entry) => selected.has(entry.sourceCaseId) && !entry.alreadyImported);
    if (!requested.length) return;
    setImporting(true); setError(""); setSummary(null);
    try {
      const response = await apiFetch(`${apiUrl}/api/v1/sources/s3/import-jobs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sourceCaseIds: requested.map((entry) => entry.sourceCaseId), autoProcess, readerMode }),
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) throw new Error(errorDetail(payload, "The selected catalog cases could not be imported."));
      const nextJob = importJob(payload);
      if (!nextJob) throw new Error("The server did not return a valid import job.");
      setJob(nextJob);
      window.localStorage.setItem(ACTIVE_JOB_KEY, nextJob.jobId);
      setSelected(new Set());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The selected catalog cases could not be imported.");
      setImporting(false);
    }
  };

  return <section className="catalog-import" aria-label="Import from shared catalog">
    <span className="eyebrow">Shared source</span>
    <h2>Import from catalog</h2>
    <p>Choose public reference cases. The service validates and copies their JSON and artwork into your assigned work queue.</p>
    {status === "loading" ? <div className="catalog-loading"><LoaderCircle className="spin" size={19} /> Loading catalog…</div> : status === "unavailable" ? <div className="catalog-unavailable"><CircleAlert size={17} /><span>{error}</span><button type="button" onClick={() => void load()}>Try again</button></div> : <>
      <div className="catalog-controls">
        <label><span>Source</span><select value={sourceFilter} onChange={(event) => { setSourceFilter(event.target.value as CatalogSourceFilter); setSelected(new Set()); }}><option value="official">Official TTB ({officialCount})</option><option value="synthetic">Test scenarios ({syntheticCount})</option><option value="all">All sources ({catalog.length})</option></select></label>
        <label><span>Category</span><select value={category} onChange={(event) => setCategory(event.target.value as BeverageCategory | "all")}><option value="all">All categories</option><option value="wine">Wine</option><option value="distilled_spirits">Distilled spirits</option><option value="malt_beverage">Malt beverage</option></select></label>
      </div>
      <div className="catalog-list-header"><label><input type="checkbox" checked={allFilteredSelected} disabled={!selectable.length} onChange={toggleAll} /> Select all</label><span>{filtered.length} shown · {filtered.filter((entry) => entry.alreadyImported).length} imported</span></div>
      <ul className="catalog-list">
        {filtered.length ? filtered.map((entry) => <li className={entry.alreadyImported ? "already-imported" : ""} key={entry.sourceCaseId}>
          <label>
            <input type="checkbox" disabled={entry.alreadyImported} checked={selected.has(entry.sourceCaseId)} onChange={() => setSelected((current) => { const next = new Set(current); if (next.has(entry.sourceCaseId)) next.delete(entry.sourceCaseId); else next.add(entry.sourceCaseId); return next; })} />
            <span><b>{entry.displayName}</b><small>{categoryLabel(entry.category)} · {entry.panelCount || 1} {entry.panelCount === 1 ? "panel" : "panels"}</small>{entry.expected?.brandName && <em>{entry.expected.brandName}{entry.expected.classType ? ` · ${entry.expected.classType}` : ""}</em>}</span>
          </label>
          {entry.alreadyImported ? <i><CheckCircle2 size={14} /> Imported</i> : <span className="catalog-entry-state">Available</span>}
        </li>) : <li className="catalog-no-results">No catalog cases match these filters.</li>}
      </ul>
      <button className="catalog-import-action" type="button" disabled={importing || selected.size === 0} onClick={() => void submit()}>{importing ? <LoaderCircle className="spin" size={16} /> : <CloudDownload size={16} />}{importing ? "Importing selected cases…" : `Import ${selected.size || ""} selected ${selected.size === 1 ? "case" : "cases"}`}</button>
      {error && <div className="catalog-submit-error" role="alert"><CircleAlert size={16} /><span>{error}</span></div>}
    </>}
    {summary && <div className="catalog-result-tray" aria-live="polite"><div><b>{summary.imported} imported</b><span>{summary.skipped} skipped</span><span>{summary.errors} errors</span></div><ul>{summary.results.slice(0, 8).map((result) => <li className={`catalog-result-${result.status}`} key={`${result.sourceCaseId}-${result.status}`}><span>{result.status === "imported" ? <CheckCircle2 size={14} /> : result.status === "error" ? <XCircle size={14} /> : <CircleAlert size={14} />}</span><b>{result.displayName ?? result.sourceCaseId}</b>{result.message && <small>{result.message}</small>}</li>)}</ul></div>}
    {job && <div className="catalog-progress-overlay" role="presentation">
      <section className="catalog-progress-dialog" role="dialog" aria-modal="true" aria-labelledby="catalog-progress-title">
        <div className={`catalog-progress-icon ${job.status}`}>
          {job.status === "complete" ? <CheckCircle2 size={24} /> : job.status === "failed" ? <XCircle size={24} /> : <LoaderCircle className="spin" size={24} />}
        </div>
        <span className="eyebrow">Catalog import</span>
        <h3 id="catalog-progress-title">{job.status === "complete" ? "Import complete" : job.status === "failed" ? "Import stopped" : job.status === "queued" ? "Waiting to import" : "Adding cases to your queue"}</h3>
        <p>{job.status === "failed" ? job.errorMessage ?? "The import could not be completed." : job.status === "complete" ? `${job.importedCaseIds.length} cases were added and ${job.duplicateCaseIds.length} were already present.` : "Keep this dialog open to monitor the import. Cases are added to the queue as they are validated."}</p>
        <div className="catalog-progress-track" role="progressbar" aria-valuemin={0} aria-valuemax={job.totalCases} aria-valuenow={job.completedCases}><span style={{ width: `${job.totalCases ? Math.round((job.completedCases / job.totalCases) * 100) : 0}%` }} /></div>
        <div className="catalog-progress-count"><b>{job.completedCases} of {job.totalCases}</b><span>{job.importedCaseIds.length} added · {job.duplicateCaseIds.length} skipped · {job.issues.length} errors</span></div>
        {(job.status === "complete" || job.status === "failed") && <button type="button" onClick={() => setJob(null)}>Done</button>}
      </section>
    </div>}
  </section>;
}
