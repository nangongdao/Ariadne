/**
 * 工作目录种子文件编辑器 —— command 断言的验证对象。
 *
 * command 断言跑的是**磁盘上的文件**，这些文件得有个来源。没有它们，
 * 断言强度会被削弱：模型可以自己生成一份恒通过的测试来"达标"。所以
 * 测试文件应当由用户提供，而不是交给被测的模型去写。
 *
 * 路径校验与后端 CreateLoopRequest._safe_workspace_paths 保持一致 ——
 * 后端仍会再挡一次（请求体不可信），这里挡是为了不让用户填完才吃 422。
 */

import { Plus, Trash2 } from "lucide-react";

export interface WorkspaceFile {
  path: string;
  content: string;
}

/** 与后端 _safe_workspace_paths 同规则。返回错误文案，合法则返回 null。 */
export function validateWorkspacePath(path: string): string | null {
  if (!path.trim()) return "路径不能为空";
  const normalized = path.replace(/\\/g, "/");
  if (normalized.startsWith("/")) return "不能使用绝对路径";
  if (normalized.split("/").includes("..")) return "不能包含 .. （目录穿越）";
  if (path.includes(":")) return "不能包含盘符或冒号";
  return null;
}

interface WorkspaceFilesFieldProps {
  files: WorkspaceFile[];
  onChange: (files: WorkspaceFile[]) => void;
}

export function WorkspaceFilesField({ files, onChange }: WorkspaceFilesFieldProps) {
  function update(index: number, patch: Partial<WorkspaceFile>) {
    onChange(files.map((f, i) => (i === index ? { ...f, ...patch } : f)));
  }

  return (
    <fieldset className="field workspace-files">
      <legend>工作目录文件</legend>
      <p className="hint">
        命令在这些文件所在的目录里执行。测试文件建议由你提供 ——
        让模型自己写测试，它可以写一个恒通过的。
      </p>

      {files.map((file, index) => {
        const pathError = file.path ? validateWorkspacePath(file.path) : null;
        return (
          <div className="workspace-file" key={index}>
            <div className="workspace-file-head">
              <input
                aria-label={`文件 ${index + 1} 路径`}
                value={file.path}
                onChange={(e) => update(index, { path: e.target.value })}
                placeholder="如：solution.py"
              />
              <button
                type="button"
                className="btn btn-sm"
                aria-label={`删除文件 ${index + 1}`}
                onClick={() => onChange(files.filter((_, i) => i !== index))}
              >
                <Trash2 size={13} aria-hidden="true" />
              </button>
            </div>
            {pathError && (
              <p className="form-error" role="alert">
                {pathError}
              </p>
            )}
            <textarea
              aria-label={`文件 ${index + 1} 内容`}
              value={file.content}
              onChange={(e) => update(index, { content: e.target.value })}
              rows={6}
              placeholder="文件内容"
              spellCheck={false}
            />
          </div>
        );
      })}

      <button
        type="button"
        className="btn btn-sm"
        onClick={() => onChange([...files, { path: "", content: "" }])}
      >
        <Plus size={13} aria-hidden="true" /> 添加文件
      </button>
    </fieldset>
  );
}
