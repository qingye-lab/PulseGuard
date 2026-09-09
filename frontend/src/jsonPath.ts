import type { ApiJsonPathOption } from "./types";

export type JsonValueType = "string" | "number" | "boolean" | "object" | "array" | "null";

export interface JsonPathNode {
  key: string;
  path: string;
  type: JsonValueType;
  preview: string;
  length: number | null;
  value: unknown;
  children?: JsonPathNode[];
}

export function parseJsonValue(raw: string): { valid: true; value: unknown } | { valid: false; value: null } {
  try {
    return { valid: true, value: JSON.parse(raw) };
  } catch {
    return { valid: false, value: null };
  }
}

export function buildJsonPathTree(value: unknown): JsonPathNode[] {
  return [
    {
      key: "$",
      path: "$",
      type: jsonValueType(value),
      preview: jsonPreview(value),
      length: jsonLength(value),
      value,
      children: buildChildren(value, "$")
    }
  ];
}

export function buildJsonPathTreeFromOptions(options: ApiJsonPathOption[] | undefined): JsonPathNode[] {
  const normalizedOptions = (options || []).filter((option) => option.path?.startsWith("$"));
  if (!normalizedOptions.length) return [];

  const root: JsonPathNode = {
    key: "$",
    path: "$",
    type: inferRootType(normalizedOptions),
    preview: "",
    length: null,
    value: undefined,
    children: []
  };
  const nodes = new Map<string, JsonPathNode>([["$", root]]);

  normalizedOptions.forEach((option) => {
    const segments = parseJsonPath(option.path);
    let parent = root;
    let currentPath = "$";
    segments.forEach((segment, index) => {
      currentPath = appendPathSegment(currentPath, segment);
      let current = nodes.get(currentPath);
      if (!current) {
        current = {
          key: currentPath,
          path: currentPath,
          type: inferPathType(segments[index + 1]),
          preview: "",
          length: null,
          value: undefined,
          children: []
        };
        nodes.set(currentPath, current);
        parent.children = [...(parent.children || []), current];
      }
      if (index === segments.length - 1) {
        current.type = option.type;
        current.preview = option.preview || "";
        current.length = option.length ?? null;
      }
      parent = current;
    });
  });

  return [root];
}

export function flattenJsonPathTree(nodes: JsonPathNode[]): JsonPathNode[] {
  const result: JsonPathNode[] = [];
  const visit = (node: JsonPathNode) => {
    result.push(node);
    node.children?.forEach(visit);
  };
  nodes.forEach(visit);
  return result;
}

export function readJsonPath(value: unknown, path: string): { exists: true; value: unknown } | { exists: false; value: null } {
  let current = value;
  for (const segment of parseJsonPath(path)) {
    if (typeof segment === "number") {
      if (!Array.isArray(current) || segment < 0 || segment >= current.length) return { exists: false, value: null };
      current = current[segment];
    } else {
      if (!current || typeof current !== "object" || Array.isArray(current) || !(segment in current)) {
        return { exists: false, value: null };
      }
      current = (current as Record<string, unknown>)[segment];
    }
  }
  return { exists: true, value: current };
}

export function jsonValueType(value: unknown): JsonValueType {
  if (value === null) return "null";
  if (Array.isArray(value)) return "array";
  if (typeof value === "object") return "object";
  if (typeof value === "number") return "number";
  if (typeof value === "boolean") return "boolean";
  return "string";
}

export function jsonLength(value: unknown): number | null {
  if (typeof value === "string" || Array.isArray(value)) return value.length;
  if (value && typeof value === "object") return Object.keys(value).length;
  return null;
}

export function jsonPreview(value: unknown, maxLength = 72): string {
  const text = typeof value === "string" ? value : JSON.stringify(value);
  if (text === undefined) return "";
  return text.length <= maxLength ? text : `${text.slice(0, maxLength - 3)}...`;
}

export function jsonLiteral(value: unknown): string {
  if (value === undefined) return "";
  if (typeof value === "string") return JSON.stringify(value);
  const encoded = JSON.stringify(value, null, 2);
  return encoded === undefined ? String(value) : encoded;
}

function buildChildren(value: unknown, parentPath: string): JsonPathNode[] | undefined {
  if (Array.isArray(value)) {
    return value.slice(0, 80).map((item, index) => node(`${parentPath}[${index}]`, item));
  }
  if (value && typeof value === "object") {
    return Object.entries(value as Record<string, unknown>).map(([key, item]) => node(childPath(parentPath, key), item));
  }
  return undefined;
}

function node(path: string, value: unknown): JsonPathNode {
  return {
    key: path,
    path,
    type: jsonValueType(value),
    preview: jsonPreview(value),
    length: jsonLength(value),
    value,
    children: buildChildren(value, path)
  };
}

function childPath(parentPath: string, key: string): string {
  if (/^[A-Za-z_$][\w$]*$/.test(key)) return `${parentPath}.${key}`;
  return `${parentPath}[${JSON.stringify(key)}]`;
}

function appendPathSegment(parentPath: string, segment: string | number): string {
  if (typeof segment === "number") return `${parentPath}[${segment}]`;
  return childPath(parentPath, segment);
}

function inferRootType(options: ApiJsonPathOption[]): JsonValueType {
  return options.some((option) => option.path.startsWith("$[")) ? "array" : "object";
}

function inferPathType(nextSegment: string | number | undefined): JsonValueType {
  return typeof nextSegment === "number" ? "array" : "object";
}

function parseJsonPath(path: string): Array<string | number> {
  const text = path.trim();
  if (!text.startsWith("$")) return [];
  const segments: Array<string | number> = [];
  let index = 1;
  while (index < text.length) {
    if (text[index] === ".") {
      index += 1;
      const start = index;
      while (index < text.length && text[index] !== "." && text[index] !== "[") index += 1;
      const key = text.slice(start, index);
      if (key) segments.push(key);
      continue;
    }
    if (text[index] === "[") {
      const end = text.indexOf("]", index);
      if (end < 0) return segments;
      const raw = text.slice(index + 1, end);
      if (/^\d+$/.test(raw)) {
        segments.push(Number(raw));
      } else {
        try {
          const key = JSON.parse(raw);
          if (typeof key === "string") segments.push(key);
        } catch {
          return segments;
        }
      }
      index = end + 1;
      continue;
    }
    break;
  }
  return segments;
}
