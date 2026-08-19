import {
  AlertTriangle,
  FileSearch,
  LoaderCircle,
  Plus,
  Save,
  ShieldCheck,
  UploadCloud,
  X,
} from "lucide-react";
import { FormEvent, useRef, useState } from "react";
import type { AdditionalExpectedField, BeverageCategory } from "../verification-types";
import type { BatchImportPreview } from "./import-case";

export type FileImportProgress = {
  completed: number;
  total: number;
  phase: "adding" | "starting_scans";
};

type EntryProps = {
  mode: "single" | "batch";
  category: BeverageCategory;
  brandName: string;
  classType: string;
  abvPercent: string;
  proof: string;
  includeGovernmentWarning: boolean;
  additionalFields: AdditionalExpectedField[];
  caseName: string;
  caseReference: string;
  file: File | null;
  additionalFiles: File[];
  autoProcess: boolean;
  error: string;
  submitting: boolean;
  importing: boolean;
  validatingBatch: boolean;
  batchPreview: BatchImportPreview | null;
  importSummary: string;
  importProgress: FileImportProgress | null;
  onCategoryChange: (value: BeverageCategory) => void;
  onBrandNameChange: (value: string) => void;
  onClassTypeChange: (value: string) => void;
  onAbvPercentChange: (value: string) => void;
  onProofChange: (value: string) => void;
  onIncludeGovernmentWarningChange: (value: boolean) => void;
  onAdditionalFieldsChange: (value: AdditionalExpectedField[]) => void;
  onCaseNameChange: (value: string) => void;
  onCaseReferenceChange: (value: string) => void;
  onAutoProcessChange: (value: boolean) => void;
  onSelectUploadedFiles: (files: File[]) => void;
  onRemovePanel: (index: number) => void;
  onMovePanel: (index: number, direction: -1 | 1) => void;
  onImport: (application: File, artwork: File[]) => void;
  onValidateBatch: (files: File[]) => void;
  onBatchImport: (preview: BatchImportPreview) => void;
  onManualSubmit: () => void;
};

export default function CaseEntryForm({
  mode,
  category,
  brandName,
  classType,
  abvPercent,
  proof,
  includeGovernmentWarning,
  additionalFields,
  caseName,
  caseReference,
  file,
  additionalFiles,
  autoProcess,
  error,
  submitting,
  importing,
  validatingBatch,
  batchPreview,
  importSummary,
  importProgress,
  onCategoryChange,
  onBrandNameChange,
  onClassTypeChange,
  onAbvPercentChange,
  onProofChange,
  onIncludeGovernmentWarningChange,
  onAdditionalFieldsChange,
  onCaseNameChange,
  onCaseReferenceChange,
  onAutoProcessChange,
  onSelectUploadedFiles,
  onRemovePanel,
  onMovePanel,
  onImport,
  onValidateBatch,
  onBatchImport,
  onManualSubmit,
}: EntryProps) {
  const [applicationFile, setApplicationFile] = useState<File | null>(null);
  const [artworkFiles, setArtworkFiles] = useState<File[]>([]);
  const [batchFiles, setBatchFiles] = useState<File[]>([]);
  const [fieldChoice, setFieldChoice] = useState("");
  const fileInputRef = useRef<HTMLInputElement>(null);

  const fieldSuggestions: Record<BeverageCategory, string[]> = {
    wine: ["Appellation", "Varietal", "Vintage", "Net contents", "Producer / bottler", "Importer", "Country of origin", "Sulfites statement"],
    distilled_spirits: ["Age statement", "Net contents", "Distiller / bottler", "Importer", "Country of origin", "State of distillation"],
    malt_beverage: ["Net contents", "Brewer / packer", "Importer", "Country of origin", "Flavor designation"],
  };

  const addExpectedField = () => {
    const label = fieldChoice.trim();
    if (!label || additionalFields.length >= 20) return;
    if (additionalFields.some((field) => field.label.toLocaleLowerCase() === label.toLocaleLowerCase())) return;
    const base = label.toLocaleLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 48) || "custom";
    let id = base.match(/^[a-z]/) ? base : `custom-${base}`;
    let suffix = 2;
    while (additionalFields.some((field) => field.id === id)) id = `${base.slice(0, 52)}-${suffix++}`;
    onAdditionalFieldsChange([...additionalFields, { id, label, expectedText: "", matchMode: "literal_phrase" }]);
    setFieldChoice("");
  };

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    onManualSubmit();
  };

  return (
    <>
      <div className="verify-intake-heading">
        <span className="eyebrow">{mode === "batch" ? "Queue import" : "New queue item"}</span>
        <h1>{mode === "batch" ? "Import from files" : "Add a case"}</h1>
        <p>{mode === "batch" ? "Add several application records and their label artwork in one step." : "Import an application record or enter one manually. Blind extraction stays separate from expected values."}</p>
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
            <button type="button" disabled={importing || submitting || validatingBatch || !batchPreview || batchPreview.ready.length === 0} onClick={() => batchPreview && onBatchImport(batchPreview)}>
              {importing ? <><LoaderCircle className="spin" size={14} /> Adding batch…</> : <><UploadCloud size={14} /> Import {batchPreview?.ready.length ?? 0} valid {batchPreview?.ready.length === 1 ? "case" : "cases"}</>}
            </button>
          </section>
          {error && <div className="verify-error"><AlertTriangle size={15} /><span>{error}</span></div>}
          {importSummary && <div className="verify-import-summary">{importSummary}</div>}
        </>
      ) : (
      <form onSubmit={submit}>
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
          <button type="button" disabled={importing || submitting || !applicationFile || !artworkFiles.length} onClick={() => applicationFile && artworkFiles.length && onImport(applicationFile, artworkFiles)}>
            {importing ? <><LoaderCircle className="spin" size={14} /> Importing…</> : <><UploadCloud size={14} /> Import files to queue</>}
          </button>
          {error && <div className="verify-error" role="alert"><AlertTriangle size={15} /><span>{error}</span></div>}
          {importSummary && <div className="verify-import-summary" role="status">{importSummary}</div>}
        </section>
        <div className="verify-intake-divider"><span>Or enter one case manually</span></div>
        <div className="verify-intake-divider"><span>Application values</span></div>
        <label>
          <span>Beverage category</span>
          <select value={category} onChange={(event) => onCategoryChange(event.target.value as BeverageCategory)}>
            <option value="distilled_spirits">Distilled spirits</option>
            <option value="wine">Wine</option>
            <option value="malt_beverage">Malt beverage</option>
          </select>
        </label>
        <label>
          <span>Expected brand name <i>may be supplied later</i></span>
          <input value={brandName} onChange={(event) => onBrandNameChange(event.target.value)} maxLength={250} />
        </label>
        <label>
          <span>Expected class / type <i>may be supplied later</i></span>
          <input value={classType} onChange={(event) => onClassTypeChange(event.target.value)} maxLength={250} />
        </label>
        <label>
          <span>Case name <i>optional</i></span>
          <input value={caseName} onChange={(event) => onCaseNameChange(event.target.value)} maxLength={250} placeholder="Defaults to the brand name" />
        </label>
        <label>
          <span>COLA case ID / reference <i>optional</i></span>
          <input value={caseReference} onChange={(event) => onCaseReferenceChange(event.target.value)} maxLength={250} placeholder="e.g. COLA-26189001000380" />
        </label>
        <div className="verify-number-grid">
          <label>
            <span>Expected ABV %</span>
            <input type="number" min="0" max="100" step="0.1" value={abvPercent} onChange={(event) => onAbvPercentChange(event.target.value)} />
          </label>
          <label>
            <span>Expected proof</span>
            <input type="number" min="0" max="200" step="0.1" value={proof} onChange={(event) => onProofChange(event.target.value)} disabled={category !== "distilled_spirits"} />
          </label>
        </div>
        <label aria-label="Verify government warning" className="verify-warning-default" htmlFor="verify-government-warning">
          <input id="verify-government-warning" type="checkbox" checked={includeGovernmentWarning} onChange={(event) => onIncludeGovernmentWarningChange(event.target.checked)} />
          <span><b>Verify government warning</b><small>Compare the label with the required statutory heading, wording, and presentation.</small></span>
        </label>
        <section className="verify-additional-fields" aria-labelledby="additional-fields-title">
          <div className="verify-additional-fields-heading">
            <span><b id="additional-fields-title">Additional expected label text</b><small>Add only values the application says should appear literally on the artwork.</small></span>
            <em>{additionalFields.length}/20</em>
          </div>
          {additionalFields.map((field, index) => (
            <div className="verify-additional-field" key={field.id}>
              <label>
                <span>Field name</span>
                <input
                  value={field.label}
                  maxLength={250}
                  onChange={(event) => onAdditionalFieldsChange(additionalFields.map((item, itemIndex) => itemIndex === index ? { ...item, label: event.target.value } : item))}
                />
              </label>
              <label>
                <span>Expected value on label</span>
                <input
                  value={field.expectedText}
                  maxLength={1000}
                  onChange={(event) => onAdditionalFieldsChange(additionalFields.map((item, itemIndex) => itemIndex === index ? { ...item, expectedText: event.target.value } : item))}
                />
              </label>
              <button type="button" aria-label={`Remove ${field.label || "additional field"}`} onClick={() => onAdditionalFieldsChange(additionalFields.filter((_, itemIndex) => itemIndex !== index))}><X size={16} /></button>
            </div>
          ))}
          <div className="verify-add-field-control">
            <label>
              <span>Add expected field</span>
              <input
                list="expected-field-suggestions"
                value={fieldChoice}
                placeholder="Choose or type a custom field name"
                onChange={(event) => setFieldChoice(event.target.value)}
                onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); addExpectedField(); } }}
              />
              <datalist id="expected-field-suggestions">{fieldSuggestions[category].map((label) => <option key={label} value={label} />)}</datalist>
            </label>
            <button type="button" disabled={!fieldChoice.trim() || additionalFields.length >= 20} onClick={addExpectedField}><Plus size={15} /> Add</button>
          </div>
        </section>
        <label className={`verify-upload ${file ? "has-file" : ""}`}>
          <UploadCloud size={22} />
          <span>{file ? `${1 + additionalFiles.length} artwork panel${additionalFiles.length ? "s" : ""} selected` : "Upload label artwork"}</span>
          <small>{file ? "Ordered panels · JPEG or PNG · up to six images" : "JPEG or PNG · one to six ordered panels · maximum 10 MB each"}</small>
          <input ref={fileInputRef} type="file" multiple accept="image/jpeg,image/png" onChange={(event) => onSelectUploadedFiles(Array.from(event.target.files ?? []).slice(0, 6))} required />
        </label>
        {file && (
          <ol className="verify-panel-list" aria-label="Ordered label panels">
            {[file, ...additionalFiles].map((panel, index) => (
              <li key={`${panel.name}-${index}`}>
                <span><b>Panel {index + 1}</b><small>{panel.name}</small></span>
                <span className="verify-panel-actions">
                  <button type="button" disabled={index === 0} onClick={() => onMovePanel(index, -1)} aria-label={`Move panel ${index + 1} earlier`}>↑</button>
                  <button type="button" disabled={index === additionalFiles.length} onClick={() => onMovePanel(index, 1)} aria-label={`Move panel ${index + 1} later`}>↓</button>
                  <button type="button" onClick={() => onRemovePanel(index)}>Remove</button>
                </span>
              </li>
            ))}
          </ol>
        )}
        <div className="verify-auto-process">
          <input id="auto-process-case" type="checkbox" checked={autoProcess} onChange={(event) => onAutoProcessChange(event.target.checked)} />
          <label htmlFor="auto-process-case"><b>Scan automatically</b><small>Start processing as soon as this case enters the queue.</small></label>
        </div>
        <button className="verify-submit" disabled={submitting} type="submit">
          {submitting ? <><LoaderCircle className="spin" size={16} /> Adding case…</> : autoProcess ? <><FileSearch size={16} /> Add to queue and scan</> : <><Save size={16} /> Add to work queue</>}
        </button>
      </form>
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
