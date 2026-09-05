import type { UiAssertion, UiAssertionType } from "./types";

const ELEMENT_SELECTOR_TYPES: UiAssertionType[] = ["element_visible", "element_hidden", "element_not_empty", "element_count"];
const TEXT_TYPES: UiAssertionType[] = ["text_present", "text_absent", "title_contains", "url_contains"];

export const UI_ASSERTION_LABELS: Record<UiAssertionType, string> = {
  element_visible: "元素可见",
  element_hidden: "元素隐藏",
  element_not_empty: "组件不为空",
  page_not_blank: "页面不空白",
  text_present: "文本出现",
  text_absent: "文本不存在",
  title_contains: "标题包含",
  url_contains: "URL 包含",
  console_error_absent: "控制台无错误",
  element_count: "元素数量"
};

const UI_ASSERTION_TYPES = Object.keys(UI_ASSERTION_LABELS) as UiAssertionType[];

export function parseUiAssertions(raw: string): UiAssertion[] {
  try {
    const parsed = JSON.parse(raw || "[]");
    if (!Array.isArray(parsed)) return [];
    return parsed.map(normalizeUiAssertion).filter(Boolean) as UiAssertion[];
  } catch {
    return [];
  }
}

export function serializeUiAssertions(assertions: UiAssertion[]): string {
  return JSON.stringify(assertions.map(normalizeUiAssertion).filter(Boolean), null, 2);
}

export function hasEnabledUiAssertions(raw: string): boolean {
  return parseUiAssertions(raw).some((assertion) => assertion.enabled);
}

export function createUiAssertion(type: UiAssertionType): UiAssertion {
  const base: UiAssertion = {
    id: `${type}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    type,
    enabled: true
  };
  if (["element_visible", "element_hidden", "element_not_empty"].includes(type)) {
    return { ...base, selector: "" };
  }
  if (type === "element_count") {
    return { ...base, selector: "", operator: "eq", expected_count: 1 };
  }
  if (TEXT_TYPES.includes(type)) {
    return { ...base, expected_text: "" };
  }
  return base;
}

function normalizeUiAssertion(item: unknown): UiAssertion | null {
  if (!item || typeof item !== "object") return null;
  const source = item as Partial<UiAssertion>;
  const type = source.type;
  if (!type || !UI_ASSERTION_TYPES.includes(type)) return null;
  const assertion: UiAssertion = {
    id: source.id || `${type}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    type,
    enabled: source.enabled !== false
  };
  if (ELEMENT_SELECTOR_TYPES.includes(type)) {
    assertion.selector = source.selector || "";
    applySelectorMetadata(assertion, source);
  }
  if (TEXT_TYPES.includes(type)) {
    assertion.expected_text = source.expected_text || "";
    if ((type === "text_present" || type === "text_absent") && source.selector) {
      assertion.selector = source.selector;
      applySelectorMetadata(assertion, source);
    }
  }
  if (type === "element_count") {
    assertion.operator = source.operator || "eq";
    assertion.expected_count = Number(source.expected_count ?? 1);
  }
  return assertion;
}

function applySelectorMetadata(assertion: UiAssertion, source: Partial<UiAssertion>) {
  const selectorType = typeof source.selector_type === "string" ? source.selector_type.trim() : "";
  const selectorStability = normalizeSelectorStability(source.selector_stability);
  const selectorScore = Number(source.selector_score);
  if (selectorType) assertion.selector_type = selectorType;
  if (selectorStability) assertion.selector_stability = selectorStability;
  if (Number.isFinite(selectorScore)) assertion.selector_score = selectorScore;
  if (typeof source.scope_selector === "string" && source.scope_selector.trim()) assertion.scope_selector = source.scope_selector.trim();
  if (typeof source.relative_selector === "string" && source.relative_selector.trim()) assertion.relative_selector = source.relative_selector.trim();
  if (source.match_mode === "first" || source.match_mode === "all" || source.match_mode === "every_scope") assertion.match_mode = source.match_mode;
  if (source.count_mode === "attached" || source.count_mode === "visible") assertion.count_mode = source.count_mode;
  if (Array.isArray(source.frame_path) && source.frame_path.every((item) => Number.isInteger(item) && item >= 0)) {
    assertion.frame_path = source.frame_path.map(Number);
  }
}

function normalizeSelectorStability(value: unknown): UiAssertion["selector_stability"] | undefined {
  return value === "high" || value === "medium" || value === "low" ? value : undefined;
}
