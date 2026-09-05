import Editor from "@monaco-editor/react";
import type { ReactNode } from "react";
import "./localMonaco";

export interface CodeEditorPanelProps {
  title: string;
  value: string;
  language: string;
  onChange: (value: string) => void;
  height?: string;
  compact?: boolean;
  extra?: ReactNode;
}

export function CodeEditorPanel({ title, value, language, onChange, height = "360px", compact = false, extra }: CodeEditorPanelProps) {
  return (
    <section className="editor-section code-editor-panel">
      <div className="section-title code-editor-header">
        <h3>{title}</h3>
        {extra}
      </div>
      <div className={`editor-frame ${compact ? "compact" : ""}`}>
        <Editor
          height={height}
          language={language}
          theme="vs"
          value={value}
          onChange={(next) => onChange(next || "")}
          options={{
            minimap: { enabled: false },
            fontSize: 13,
            fontFamily: "var(--font-code)",
            lineNumbersMinChars: 3,
            scrollBeyondLastLine: false,
            tabSize: 2,
            wordWrap: "on",
            automaticLayout: true
          }}
        />
      </div>
    </section>
  );
}
