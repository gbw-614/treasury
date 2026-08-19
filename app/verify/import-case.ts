import type { AdditionalExpectedField, BeverageCategory, FieldLibraryCheck } from "../verification-types";

export type ExpectedGovernmentWarning = {
  heading: string;
  body: string;
};

export type ImportedCase = {
  displayName: string;
  request: {
    schemaVersion: "verification-request-v1";
    caseReference: string | null;
    category: BeverageCategory;
    expected: {
      brandName: string | null;
      classType: string | null;
      abvPercent: number | null;
      proof: number | null;
      governmentWarning: ExpectedGovernmentWarning | null;
      additionalFields: AdditionalExpectedField[];
    };
    panels: Array<{ panelId: string; file: string }>;
  } | {
    schemaVersion: "verification-request-v2";
    caseReference: string | null;
    category: BeverageCategory;
    checks: FieldLibraryCheck[];
    panels: Array<{ panelId: string; file: string }>;
  };
};

export type ImportFilePair = {
  key: string;
  application: File;
  artwork: File[];
};

export type BatchImportSelection = {
  pairs: ImportFilePair[];
  errors: string[];
};

export type BatchImportPreview = {
  ready: Array<ImportFilePair & { imported: ImportedCase }>;
  issues: string[];
};

function fileStem(name: string, kind: "application" | "artwork") {
  const withoutExtension = name.replace(/\.[^.]+$/, "");
  return (kind === "application"
    ? withoutExtension.replace(/\.(application|case)$/i, "")
    : withoutExtension.replace(/\.(p\d+|front|back|neck|side|panel\d+)$/i, "")
  ).trim().toLocaleLowerCase();
}

function fileExtension(name: string) {
  const match = /\.([^.]+)$/.exec(name);
  return match?.[1]?.toLocaleLowerCase() ?? "";
}

/**
 * Pairs application JSON and artwork by their case-insensitive filename stem.
 * For example, `north-point.json` (or `north-point.application.json`) pairs
 * with `north-point.png`.
 */
export function pairBatchImportFiles(files: File[]): BatchImportSelection {
  const grouped = new Map<string, { displayName: string; applications: File[]; artwork: File[] }>();
  const errors: string[] = [];

  for (const file of files) {
    const extension = fileExtension(file.name);
    const kind = extension === "json" ? "applications" : ["jpg", "jpeg", "png"].includes(extension) ? "artwork" : null;
    if (!kind) {
      errors.push(`${file.name}: select JSON, PNG, or JPEG files only.`);
      continue;
    }
    const key = fileStem(file.name, kind === "applications" ? "application" : "artwork");
    if (!key) {
      errors.push(`${file.name}: the filename needs a name before its extension.`);
      continue;
    }
    const group = grouped.get(key) ?? { displayName: file.name.replace(/\.[^.]+$/, ""), applications: [], artwork: [] };
    group[kind].push(file);
    grouped.set(key, group);
  }

  const pairs: ImportFilePair[] = [];
  for (const [key, group] of [...grouped.entries()].sort(([left], [right]) => left.localeCompare(right))) {
    if (group.applications.length !== 1 || group.artwork.length < 1 || group.artwork.length > 6) {
      if (group.applications.length === 0) errors.push(`${group.displayName}: artwork is missing its matching JSON file.`);
      else if (group.artwork.length === 0) errors.push(`${group.displayName}: application JSON is missing its matching image.`);
      else if (group.artwork.length > 6) errors.push(`${group.displayName}: a label set can contain at most six images.`);
      else errors.push(`${group.displayName}: use exactly one application JSON file.`);
      continue;
    }
    pairs.push({
      key,
      application: group.applications[0],
      artwork: [...group.artwork].sort((left, right) => left.name.localeCompare(right.name)),
    });
  }
  return { pairs, errors };
}

/** Validate the batch locally before any case or artwork is written to the queue. */
export async function previewBatchImportFiles(files: File[]): Promise<BatchImportPreview> {
  const selection = pairBatchImportFiles(files);
  const ready: BatchImportPreview["ready"] = [];
  const issues = [...selection.errors];

  for (const pair of selection.pairs) {
    try {
      const imported = parseCaseImport(JSON.parse(await pair.application.text()), pair.artwork.map((file) => file.name));
      ready.push({ ...pair, imported });
    } catch (caught) {
      const message = caught instanceof SyntaxError
        ? "application file is not valid JSON"
        : caught instanceof Error ? caught.message : "could not be validated";
      issues.push(`${pair.application.name}: ${message}`);
    }
  }
  return { ready, issues };
}

function optionalText(value: unknown, label: string) {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value !== "string") throw new Error(`The import JSON has an invalid ${label}.`);
  return value.trim() || null;
}

function optionalNumber(value: unknown, label: string, maximum: number) {
  if (value === null || value === undefined || value === "") return null;
  const parsed = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(parsed) || parsed < 0 || parsed > maximum) {
    throw new Error(`The import JSON has an invalid ${label}.`);
  }
  return parsed;
}

function category(value: unknown): BeverageCategory {
  if (value === "wine" || value === "distilled_spirits" || value === "malt_beverage") {
    return value;
  }
  throw new Error("The import JSON category must be wine, distilled_spirits, or malt_beverage.");
}

function governmentWarning(value: unknown): ExpectedGovernmentWarning | null {
  if (value === null || value === undefined) return null;
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("The import JSON expected.governmentWarning must be an object when provided.");
  }
  const warning = value as Record<string, unknown>;
  const heading = optionalText(warning.heading, "expected.governmentWarning.heading");
  const body = optionalText(warning.body, "expected.governmentWarning.body");
  if (!heading || !body) {
    throw new Error("The import JSON expected.governmentWarning needs both heading and body.");
  }
  return { heading, body };
}

function additionalFields(value: unknown): AdditionalExpectedField[] {
  if (value === null || value === undefined) return [];
  if (!Array.isArray(value) || value.length > 20) {
    throw new Error("The import JSON expected.additionalFields must be an array with at most 20 entries.");
  }
  const parsed = value.map((item, index) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      throw new Error(`The import JSON expected.additionalFields[${index}] must be an object.`);
    }
    const field = item as Record<string, unknown>;
    const id = optionalText(field.id, `expected.additionalFields[${index}].id`);
    const label = optionalText(field.label, `expected.additionalFields[${index}].label`);
    const expectedText = optionalText(field.expectedText ?? field.expected_text, `expected.additionalFields[${index}].expectedText`);
    const matchMode = field.matchMode ?? field.match_mode ?? "literal_phrase";
    if (!id || !/^[a-z][a-z0-9_-]{0,59}$/.test(id)) {
      throw new Error(`The import JSON expected.additionalFields[${index}].id is invalid.`);
    }
    if (!label || !expectedText || matchMode !== "literal_phrase") {
      throw new Error(`The import JSON expected.additionalFields[${index}] needs label, expectedText, and literal_phrase matching.`);
    }
    return { id, label, expectedText, matchMode } as AdditionalExpectedField;
  });
  if (new Set(parsed.map((field) => field.id)).size !== parsed.length) {
    throw new Error("The import JSON expected.additionalFields ids must be unique.");
  }
  if (new Set(parsed.map((field) => field.label.trim().toLocaleLowerCase())).size !== parsed.length) {
    throw new Error("The import JSON expected.additionalFields labels must be unique.");
  }
  return parsed;
}

function fieldLibraryChecks(value: unknown): FieldLibraryCheck[] {
  if (!Array.isArray(value) || value.length === 0 || value.length > 20) {
    throw new Error("The import JSON checks must be an array with one to 20 selected fields.");
  }
  const checks = value.map((item, index) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      throw new Error(`The import JSON checks[${index}] must be an object.`);
    }
    const check = item as Record<string, unknown>;
    const fieldId = optionalText(check.fieldId ?? check.field_id, `checks[${index}].fieldId`);
    if (!fieldId || !/^[a-z][a-z0-9_]{0,59}$/.test(fieldId)) {
      throw new Error(`The import JSON checks[${index}].fieldId is invalid.`);
    }
    if (typeof check.required !== "boolean") {
      throw new Error(`The import JSON checks[${index}].required must be true or false.`);
    }
    const expectedValue = optionalText(check.expectedValue ?? check.expected_value, `checks[${index}].expectedValue`);
    return { fieldId, required: check.required, expectedValue };
  });
  if (new Set(checks.map((check) => check.fieldId)).size !== checks.length) {
    throw new Error("Every selected field-library field must be unique.");
  }
  return checks;
}

function importDisplayName(input: Record<string, unknown>, brandFallback: string | null | undefined) {
  for (const value of [input.displayName, input.applicationId, input.title]) {
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return brandFallback ?? "Imported COLA case";
}

function importPanels(artworkFiles: string[]) {
  return artworkFiles.map((file, index) => ({ panelId: `p${String(index + 1).padStart(2, "0")}`, file }));
}

export function parseCaseImport(payload: unknown, artworkFile: string | string[]): ImportedCase {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("The application file must contain a JSON object.");
  }
  const input = payload as Record<string, unknown>;
  const isV2Import = input.schemaVersion === "verification-request-v2";
  const isCurrentImport = input.schemaVersion === "verification-case-import-v1" || input.schemaVersion === "verification-request-v1";
  const isBundledSample = input.schemaVersion === "sample-case-v1";
  if (!isCurrentImport && !isBundledSample && !isV2Import) {
    throw new Error("Use verification-case-import-v1, verification-request-v1, verification-request-v2, or a bundled sample-case-v1 fixture.");
  }
  if (!isV2Import && (!input.expected || typeof input.expected !== "object" || Array.isArray(input.expected))) {
    throw new Error("The import JSON needs an expected object.");
  }
  const parsedCategory = category(input.category);
  const caseReference = optionalText(input.caseReference ?? input.case_reference, "caseReference");
  const artworkFiles = Array.isArray(artworkFile) ? artworkFile : [artworkFile];
  if (!artworkFiles.length || artworkFiles.length > 6) {
    throw new Error("Choose between one and six artwork panels.");
  }
  if (isV2Import) {
    const checks = fieldLibraryChecks(input.checks);
    const brand = checks.find((check) => check.fieldId === "brand_name")?.expectedValue;
    return {
      displayName: importDisplayName(input, brand),
      request: {
        schemaVersion: "verification-request-v2",
        caseReference,
        category: parsedCategory,
        checks,
        panels: importPanels(artworkFiles),
      },
    };
  }
  const expected = input.expected as Record<string, unknown>;
  const brandName = optionalText(expected.brandName, "expected.brandName");
  const classType = optionalText(expected.classType, "expected.classType");
  const abvPercent = optionalNumber(expected.abvPercent, "expected.abvPercent", 100);
  const proof = optionalNumber(expected.proof, "expected.proof", 200);
  const parsedGovernmentWarning = governmentWarning(expected.governmentWarning ?? expected.government_warning);
  const parsedAdditionalFields = additionalFields(expected.additionalFields ?? expected.additional_fields);
  return {
    displayName: importDisplayName(input, brandName),
    request: {
      schemaVersion: "verification-request-v1",
      caseReference,
      category: parsedCategory,
      expected: { brandName, classType, abvPercent, proof, governmentWarning: parsedGovernmentWarning, additionalFields: parsedAdditionalFields },
      panels: importPanels(artworkFiles),
    },
  };
}
