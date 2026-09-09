// Monaco 0.55's language entrypoints register the complete editor feature set.
// Keep core Python/JSON/plaintext editing, completion, find, hover, folding and
// formatting; omit optional IDE features such as inline suggestions, lenses,
// color pickers and sticky scroll. Exports used by retained features still
// participate in normal tree shaking.
const unusedContributions = new Set([
  "anchorSelect", "codeAction", "codelens", "colorPicker", "diffEditorBreadcrumbs",
  "dropOrPasteInto", "floatingMenu", "fontZoom", "gpu", "inlayHints",
  "inlineCompletions", "inlineProgress", "inPlaceReplace", "linkedEditing",
  "middleScroll", "parameterHints", "rename", "sectionHeaders", "semanticTokens",
  "stickyScroll"
]);

export function monacoModuleSideEffects(id) {
  const normalized = id.replaceAll("\\", "/");
  const contribution = normalized.match(/\/monaco-editor\/esm\/vs\/editor\/contrib\/([^/]+)\//);
  if (contribution && unusedContributions.has(contribution[1])) return false;
  if (/\/monaco-editor\/esm\/vs\/editor\/browser\/widget\/diffEditor\/diffEditor\.contribution\.js$/.test(normalized)) return false;
  if (/\/monaco-editor\/esm\/vs\/editor\/standalone\/browser\/(?:iPadShowKeyboard|inspectTokens)\//.test(normalized)) return false;
  return true;
}
