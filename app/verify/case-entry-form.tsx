import { AlertTriangle, LoaderCircle, ShieldCheck, UploadCloud } from "lucide-react";
import { useState } from "react";
import type { BatchImportPreview } from "./import-case";

export type FileImportProgress = {
  completed: number;
  total: number;
  phase: "adding" | "starting_scans";
};

type EntryProps = {
  mode: "single" | "batch";
  autoProcess: boolean;
  error: string;
  importing: boolean;
  validatingBatch: boolean;
  batchPreview: BatchImportPreview | null;
  importSummary: string;
  importProgress: FileImportProgress | null;
  onAutoProcessChange: (value: boolean) => void;
  onImport: (application: File, artwork: File[]) => void;
  onValidateBatch: (files: File[]) => void;
  onBatchImport: (preview: BatchImportPreview) => void;
};

/**
 * Queue intake deliberately accepts application JSON plus artwork only. The
 * expected checks belong to the application record; there is no manual case
 * construction path in the product.
 */
export default function CaseEntryForm({
  mode,
  autoProcess,
  error,
  importing,
  validatingBatch,
  batchPreview,
  importSummary,
  importProgress,
  onAutoProcessChange,
  onImport,
  onValidateBatch,
  onBatchImport,
}: EntryProps) {
  const [applicationFile, setApplicationFile] = useState<File | null>(null);
  const [artworkFiles, setArtworkFiles] = useState<File[]>([]);
  const [batchFiles, setBatchFiles] = useState<File[]>([]);

  return (
    <>
      <div className="verify-intake-heading">
        <span className="eyebrow">{mode === "batch" ? "Queue import" : "New queue item"}</span>
        <h1>{mode === "batch" ? "Import from files" : "Add a case"}</h1>
        <p>{mode === "batch" ? "Add several application records and their label artwork in one step." : "Import an application record and its label artwork. Blind extraction stays separate from expected values."}</p>
      </div>
      {mode === "batch" ? (
        <>
          <section className="verify-batch-import verify-batch-import-standalone" aria-label="Batch import application and artwork files">
            <b>Paired application files</b>
            <p>Select all JSON and image files together. Files pair by case name: <code>north-point.application.json</code> + <code>north-point.front.png</code> + <code>north-point.back.png</code>.</p>
            <label>
              <input type="file" multiple accept="application/json,.json,image/jpeg,image/png" onChange={(event) => { const files = Array.from(event.target.files ?? []); setBatchFiles(files); onValidateBatch(files); }} />
              <span>{batchFiles.length ? `${batchFiles.length} files selected` : "Choose JSON and image files"}</span>
            </label>
            {validatingBatch && <div className="verify-batch-checking"><LoaderCircle className="spin" size={14} /> Checking selected files…</div>}
            {batchPreview && !validatingBatch && (
              <div className="verify-batch-preview">
                <strong>{batchPreview.ready.length} {batchPreview.ready.length === 1 ? "valid pair" : "valid pairs"} ready to import</strong>
                {batchPreview.issues.length > 0 && (
                  <details className="verify-exception-tray" open>
                    <summary>{batchPreview.issues.length} {batchPreview.issues.length === 1 ? "file needs" : "files need"} attention</summary>
                    <ul>{batchPreview.issues.map((issue) => <li key={issue}>{issue}</li>)}</ul>
                  </details>
                )}
              </div>
            )}
            <div className="verify-auto-process">
              <input id="auto-process-batch" type="checkbox" checked={autoProcess} onChange={(event) => onAutoProcessChange(event.target.checked)} />
              <label htmlFor="auto-process-batch"><b>Scan automatically</b><small>Start processing valid cases as soon as they enter the queue.</small></label>
            </div>
            <button type="button" disabled={importing || validatingBatch || !batchPreview || batchPreview.ready.length === 0} onClick={() => batchPreview && onBatchImport(batchPreview)}>
              {importing ? <><LoaderCircle className="spin" size={14} /> Adding batch…</> : <><UploadCloud size={14} /> Import {batchPreview?.ready.length ?? 0} valid {batchPreview?.ready.length === 1 ? "case" : "cases"}</>}
            </button>
          </section>
          {error && <div className="verify-error"><AlertTriangle size={15} /><span>{error}</span></div>}
          {importSummary && <div className="verify-import-summary">{importSummary}</div>}
        </>
      ) : (
        <section className="verify-import-picker" aria-label="Import application and artwork files">
          <span className="verify-sample-heading"><UploadCloud size={14} /><b>Import one case</b></span>
          <p>Choose an application JSON file and its label artwork to add a case directly to the queue.</p>
          <div className="verify-import-files">
            <label>
              <span>Application JSON</span>
              <input type="file" accept="application/json,.json" onChange={(event) => setApplicationFile(event.target.files?.[0] ?? null)} />
              <small>{applicationFile?.name ?? "Choose .json"}</small>
            </label>
            <label>
              <span>Artwork panels</span>
              <input type="file" multiple accept="image/jpeg,image/png" onChange={(event) => setArtworkFiles(Array.from(event.target.files ?? []).slice(0, 6))} />
              <small>{artworkFiles.length ? `${artworkFiles.length} panel${artworkFiles.length === 1 ? "" : "s"} selected` : "Choose one to six JPEGs or PNGs"}</small>
            </label>
          </div>
          <div className="verify-auto-process">
            <input id="auto-process-single" type="checkbox" checked={autoProcess} onChange={(event) => onAutoProcessChange(event.target.checked)} />
            <label htmlFor="auto-process-single"><b>Scan automatically</b><small>Start processing this case as soon as it enters the queue.</small></label>
          </div>
          <button type="button" disabled={importing || !applicationFile || !artworkFiles.length} onClick={() => applicationFile && artworkFiles.length && onImport(applicationFile, artworkFiles)}>
            {importing ? <><LoaderCircle className="spin" size={14} /> Importing…</> : <><UploadCloud size={14} /> Import files to queue</>}
          </button>
          {error && <div className="verify-error" role="alert"><AlertTriangle size={15} /><span>{error}</span></div>}
          {importSummary && <div className="verify-import-summary" role="status">{importSummary}</div>}
        </section>
      )}
      <div className="verify-disclosure"><ShieldCheck size={15} /><span><b>Connected analysis</b>The Python service runs the selected reader. LLM mode sends only the artwork—not the expected values—to the configured vision provider.</span></div>
      {importProgress && (
        <div className="file-import-progress-overlay" role="presentation">
          <section className="file-import-progress-dialog" role="status" aria-live="polite" aria-labelledby="file-import-progress-title">
            <LoaderCircle className="spin" size={25} />
            <span className="eyebrow">File import</span>
            <h2 id="file-import-progress-title">{importProgress.phase === "adding" ? "Adding cases to the queue" : "Starting recognition"}</h2>
            <p>{importProgress.phase === "adding" ? "Each application and its artwork are being saved to your queue." : "Cases are queued in visible order for recognition."}</p>
            <strong>{importProgress.completed} of {importProgress.total}</strong>
            <div className="file-import-progress-track" aria-hidden="true"><span style={{ width: `${importProgress.total ? (importProgress.completed / importProgress.total) * 100 : 0}%` }} /></div>
          </section>
        </div>
      )}
    </>
  );
}
