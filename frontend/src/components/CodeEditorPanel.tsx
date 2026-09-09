import Editor, { type OnMount } from "@monaco-editor/react";
import type { ReactNode } from "react";
import { useCallback, useEffect, useRef } from "react";
import type { editor, Position } from "monaco-editor";
import "./localMonaco";
import type { ModuleCompletionItem } from "../moduleLibrary";

// @monaco-editor/react calls updateOptions whenever this object's identity
// changes. Keep typing and parent form updates off that configuration path.
const EDITOR_OPTIONS: editor.IStandaloneEditorConstructionOptions = {
  minimap: { enabled: false },
  fontSize: 13,
  fontFamily: "var(--font-code)",
  lineNumbersMinChars: 3,
  scrollBeyondLastLine: false,
  tabSize: 2,
  wordWrap: "on",
  automaticLayout: true
};
const EMPTY_COMPLETIONS: ModuleCompletionItem[] = [];

export interface CodeEditorPanelProps {
  title: string;
  value: string;
  language: string;
  onChange: (value: string) => void;
  height?: string;
  compact?: boolean;
  extra?: ReactNode;
  moduleCompletions?: ModuleCompletionItem[];
  completionScopeKey?: string;
}

export function CodeEditorPanel({
  title,
  value,
  language,
  onChange,
  height = "360px",
  compact = false,
  extra,
  moduleCompletions = EMPTY_COMPLETIONS,
  completionScopeKey = ""
}: CodeEditorPanelProps) {
  const completionsRef = useRef(moduleCompletions);
  const completionDisposableRef = useRef<{ dispose: () => void } | null>(null);
  const editorRef = useRef<{ getModel: () => unknown } | null>(null);

  useEffect(() => {
    completionsRef.current = moduleCompletions;
  }, [moduleCompletions]);

  useEffect(() => {
    return () => {
      completionDisposableRef.current?.dispose();
      completionDisposableRef.current = null;
      editorRef.current = null;
    };
  }, []);

  const handleMount = useCallback<OnMount>((editor, monaco) => {
    completionDisposableRef.current?.dispose();
    completionDisposableRef.current = null;
    editorRef.current = editor;
    if (language !== "python") return;

    completionDisposableRef.current = monaco.languages.registerCompletionItemProvider(language, {
      triggerCharacters: [".", '"', "'"],
      provideCompletionItems: (model: editor.ITextModel, position: Position) => {
        if (model !== editor.getModel() || !completionsRef.current.length) return { suggestions: [] };
        const word = model.getWordUntilPosition(position);
        const range = {
          startLineNumber: position.lineNumber,
          endLineNumber: position.lineNumber,
          startColumn: word.startColumn,
          endColumn: position.column
        };
        return {
          suggestions: completionsRef.current.map((item) => ({
            label: item.label,
            kind: item.kind === "function" ? monaco.languages.CompletionItemKind.Function : monaco.languages.CompletionItemKind.Module,
            insertText: item.insertText,
            insertTextRules: item.insertText.includes("${")
              ? monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet
              : undefined,
            detail: item.detail,
            documentation: item.documentation,
            filterText: item.label,
            range
          }))
        };
      }
    });
  }, [language]);

  return (
    <section className="editor-section code-editor-panel">
      <div className="section-title code-editor-header">
        <h3>{title}</h3>
        {extra}
      </div>
      <div className={`editor-frame ${compact ? "compact" : ""}`}>
        <Editor
          key={`${language}:${completionScopeKey}`}
          height={height}
          language={language}
          theme="vs"
          value={value}
          onChange={(next) => onChange(next || "")}
          onMount={handleMount}
          options={EDITOR_OPTIONS}
        />
      </div>
    </section>
  );
}
