/**
 * 图编辑器工具栏 —— 名称、描述、脏标记与三个动作。
 *
 * 描述必须在这里可编辑：后端 PUT 用的是 GraphCreateRequest，description 缺省为
 * ""，页面不回传就等于每次保存都把它抹掉，而列表页正显示这一列。
 */

import { Check } from "lucide-react";
import { useId, type RefObject } from "react";

interface GraphToolbarProps {
  name: string;
  description: string;
  nameError: string | null;
  dirty: boolean;
  isNew: boolean;
  isValidating: boolean;
  isSaving: boolean;
  nameInputRef: RefObject<HTMLInputElement | null>;
  onNameChange: (value: string) => void;
  onDescriptionChange: (value: string) => void;
  onValidate: () => void;
  onSave: () => void;
  onBack: () => void;
}

export function GraphToolbar({
  name,
  description,
  nameError,
  dirty,
  isNew,
  isValidating,
  isSaving,
  nameInputRef,
  onNameChange,
  onDescriptionChange,
  onValidate,
  onSave,
  onBack,
}: GraphToolbarProps) {
  const nameInputId = useId();
  const descInputId = useId();

  return (
    <div className="graph-toolbar">
      <div className="graph-toolbar-field">
        <label htmlFor={nameInputId} className="config-label">
          图名称
        </label>
        <input
          id={nameInputId}
          ref={nameInputRef}
          type="text"
          value={name}
          onChange={(e) => onNameChange(e.target.value)}
          placeholder="图名称"
          className="graph-name-input"
          maxLength={100}
          aria-invalid={nameError !== null}
        />
      </div>

      <div className="graph-toolbar-field">
        <label htmlFor={descInputId} className="config-label">
          描述
        </label>
        <input
          id={descInputId}
          type="text"
          value={description}
          onChange={(e) => onDescriptionChange(e.target.value)}
          placeholder="这张图做什么（可选）"
          className="graph-desc-input"
        />
      </div>

      {dirty && (
        <span className="badge warn" title="有未保存的修改">
          未保存
        </span>
      )}

      <button type="button" className="btn" onClick={onValidate} disabled={isValidating}>
        {isValidating ? (
          "校验中…"
        ) : (
          <>
            <Check size={13} aria-hidden />
            校验
          </>
        )}
      </button>
      <button type="button" className="btn btn-primary" onClick={onSave} disabled={isSaving}>
        {isSaving ? "保存中…" : isNew ? "创建" : "保存"}
      </button>
      <button type="button" className="btn" onClick={onBack}>
        返回
      </button>
    </div>
  );
}
