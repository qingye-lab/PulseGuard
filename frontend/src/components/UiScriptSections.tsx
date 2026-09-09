import { Button, Collapse, Tooltip } from "antd";
import { uiScriptTemplate, uiSetupScriptTemplate } from "../defaults";
import { LazyCodeEditorPanel as CodeEditorPanel } from "./LazyCodeEditorPanel";
import type { ModuleCompletionItem } from "../moduleLibrary";

interface UiScriptSectionsProps {
  setupScript: string;
  script: string;
  setupHeight?: string;
  scriptHeight?: string;
  onSetupScriptChange: (value: string) => void;
  onScriptChange: (value: string) => void;
  moduleCompletions?: ModuleCompletionItem[];
  completionScopeKey?: string;
}

export function UiScriptSections({
  setupScript,
  script,
  setupHeight = "280px",
  scriptHeight = "320px",
  onSetupScriptChange,
  onScriptChange,
  moduleCompletions = [],
  completionScopeKey = ""
}: UiScriptSectionsProps) {
  return (
    <Collapse
      className="advanced-script-collapse"
      items={[
        {
          key: "setup",
          label: <Tooltip title="先执行脚本，再加载入口 URL。使用 ctx.vars.set('skuId', value) 生成临时变量，URL 中用 ${skuId} 引用。">前置脚本</Tooltip>,
          children: (
            <CodeEditorPanel
              title="前置 Python 脚本"
              language="python"
              value={setupScript}
              height={setupHeight}
              onChange={onSetupScriptChange}
              moduleCompletions={moduleCompletions}
              completionScopeKey={`${completionScopeKey}:setup`}
              extra={
                !setupScript.trim() && (
                  <Button size="small" onClick={() => onSetupScriptChange(uiSetupScriptTemplate)}>
                    使用模板
                  </Button>
                )
              }
            />
          )
        },
        {
          key: "script",
          label: "高级探测脚本",
          children: (
            <CodeEditorPanel
              title="Python 脚本"
              language="python"
              value={script}
              height={scriptHeight}
              onChange={onScriptChange}
              moduleCompletions={moduleCompletions}
              completionScopeKey={`${completionScopeKey}:script`}
              extra={
                !script.trim() && (
                  <Button size="small" onClick={() => onScriptChange(uiScriptTemplate)}>
                    使用模板
                  </Button>
                )
              }
            />
          )
        }
      ]}
    />
  );
}
