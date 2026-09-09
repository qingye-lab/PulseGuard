import type { ModuleLibrary, ModuleExport, ProjectModule } from "./types";

export interface ModuleCompletionItem {
  label: string;
  insertText: string;
  detail: string;
  documentation: string;
  kind: "module" | "function";
}

export function moduleCompletionItems(library: ModuleLibrary | null): ModuleCompletionItem[] {
  if (!library) return [];
  const items: ModuleCompletionItem[] = [];
  const modules = library.effective_modules;
  for (const module of modules) {
    items.push({
      label: `ctx.modules.use("${module.name}")`,
      insertText: `ctx.modules.use("${module.name}")`,
      detail: "公共方法模块",
      documentation: module.description?.trim() || `使用项目公共方法模块 ${module.name}`,
      kind: "module"
    });
    for (const exported of module.exports || []) {
      items.push({
        label: `${module.name}.${exported.name}`,
        insertText: moduleCallSnippet(module.name, exported),
        detail: exported.signature || "公共方法",
        documentation: exported.docstring?.trim() || `调用 ${module.name}.${exported.name}`,
        kind: "function"
      });
    }
  }
  return items;
}

export function moduleCallSnippet(moduleName: string, exported: ModuleExport): string {
  const expression = `ctx.modules.use(${JSON.stringify(moduleName)}).${exported.name}`;
  if (exported.kind === "constant") return expression;
  const positional = (exported.positional || []).slice(0, exported.required_positional || 0);
  const positionalPlaceholders = positional.map((name, index) => `\${${index + 1}:${name}}`);
  const requiredKeywordOnly = new Set(exported.required_keyword_only || []);
  const keywordOnly = (exported.keyword_only || []).filter((name) => requiredKeywordOnly.has(name));
  const keywordPlaceholders = keywordOnly.map((name, index) => `${name}=\${${positionalPlaceholders.length + index + 1}:${name}}`);
  const argumentsText = [...positionalPlaceholders, ...keywordPlaceholders].join(", ");
  const invocation = `${expression}(${argumentsText})`;
  return exported.kind === "async" ? `await ${invocation}` : invocation;
}

export function findModule(library: ModuleLibrary | null, name: string): ProjectModule | null {
  const modules = library?.effective_modules ?? [];
  return modules.find((module) => module.name === name) || null;
}
