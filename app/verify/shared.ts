import type { BeverageCategory, QueuePreferences } from "../verification-types";

export const DEFAULT_QUEUE_PREFERENCES: QueuePreferences = {
  query: "", outcomeFilter: "all", reviewFilter: "all", assignmentFilter: "all", showRemoved: false,
  reviewWorkspaceFilter: "review_only",
};

export function categoryLabel(category: BeverageCategory | null, nullLabel = "—") {
  if (category === null) return nullLabel;
  if (category === "distilled_spirits") return "Distilled spirits";
  if (category === "malt_beverage") return "Malt beverage";
  return "Wine";
}

export function errorMessage(payload: unknown, fallback: string) {
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
