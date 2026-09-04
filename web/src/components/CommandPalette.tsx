/**
 * Ctrl+K 命令面板 —— 开发者工具标配的全局快速跳转。
 *
 * 交互范式（参考 designprompts.dev 收录的工具类产品）：
 * 居中模态 + 背景模糊，输入即过滤（label/group/keywords），
 * 全键盘操作：↑↓ 选择、↵ 跳转、Esc 关闭。
 * 除背景遮罩淡入外，动效只用 transform/opacity。
 */

import { Activity, Plus, Repeat, Search, Workflow } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { shortId } from "@/lib/format";
import { navigationGuard } from "@/lib/navigation-guard";
import { NAV_GROUPS } from "@/nav";

interface Command {
  id: string;
  label: string;
  group: string;
  keywords: string;
  icon: LucideIcon;
  to: string;
  state?: Record<string, unknown>;
  hint?: string;
}

const NAV_COMMANDS: Command[] = NAV_GROUPS.flatMap((group) =>
  group.items.map((item) => ({
    id: `nav:${item.to}`,
    label: item.label,
    group: group.title,
    keywords: item.keywords ?? "",
    icon: item.icon,
    to: item.to,
  })),
);

const ACTION_COMMANDS: Command[] = [
  {
    id: "action:new-model",
    label: "新建模型配置",
    group: "操作",
    keywords: "new model llm api key apikey baseurl 模型 新建 创建",
    icon: Plus,
    to: "/models",
    state: { openForm: true },
    hint: "新建",
  },
];

const ALL_COMMANDS = [...NAV_COMMANDS, ...ACTION_COMMANDS];

const HEX32 = /^[0-9a-f]{32}$/i;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * 粘进来的 ID 直接给跳转项。
 *
 * 前端无法从 ID 本身断定它是 Trace 还是 Loop（都可能是 UUID），所以
 * 两个入口都列出来让人选，而不是猜一个然后跳到 404。OTel 的 32 位
 * 十六进制只可能是 Trace，这种情况下就不必多问。
 */
function idCommands(raw: string): Command[] {
  const id = raw.trim();
  if (id.length < 8) return [];

  const isTrace = HEX32.test(id);
  const isUuid = UUID.test(id);
  if (!isTrace && !isUuid) return [];

  const jumps: Command[] = [
    {
      id: `jump:trace:${id}`,
      label: `打开 Trace ${shortId(id, 12)}`,
      group: "跳转",
      keywords: id,
      icon: Activity,
      to: `/traces/${id}`,
      hint: "Trace",
    },
  ];
  if (isUuid) {
    jumps.push({
      id: `jump:loop:${id}`,
      label: `打开 Loop ${shortId(id, 12)}`,
      group: "跳转",
      keywords: id,
      icon: Repeat,
      to: `/loops/${id}`,
      hint: "Loop",
    });
    // UUID 可能是 Loop 或图（都是 UUID，前端无法从格式区分），
    // 把两者都列出来让人选，而不是猜一个然后跳 404
    jumps.push({
      id: `jump:graph:${id}`,
      label: `打开图 ${shortId(id, 12)}`,
      group: "跳转",
      keywords: id,
      icon: Workflow,
      to: `/graphs/${id}`,
      hint: "图",
    });
    jumps.reverse();
  }
  return jumps;
}

/** 子序列匹配：输入 "mxpz" 能命中 "模型配置"的拼音缩写场景之外，
    也让 "trcl" 命中 "trace 链路"。比 includes 更接近 cmdk 的手感 */
function subsequence(haystack: string, needle: string): boolean {
  let i = 0;
  for (const ch of haystack) {
    if (ch === needle[i]) i += 1;
    if (i === needle.length) return true;
  }
  return needle.length === 0;
}

export function CommandPalette(props: { open: boolean; onClose: () => void }) {
  const { open, onClose } = props;
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const results = useMemo(() => {
    const q = query.trim().toLowerCase();
    const jumps = idCommands(query);
    if (!q) return ALL_COMMANDS;
    const matched = ALL_COMMANDS.filter((cmd) => {
      const hay = `${cmd.label} ${cmd.group} ${cmd.keywords}`.toLowerCase();
      return hay.includes(q) || subsequence(hay, q);
    });
    // ID 跳转排在最前：粘 ID 的意图比模糊搜页面明确得多
    return [...jumps, ...matched];
  }, [query]);

  // 打开时重置状态并聚焦输入框；关闭时把焦点还给原来的元素
  useEffect(() => {
    if (!open) return;
    const opener = document.activeElement as HTMLElement | null;
    setQuery("");
    setActive(0);
    const timer = window.setTimeout(() => inputRef.current?.focus(), 0);
    return () => {
      window.clearTimeout(timer);
      opener?.focus?.();
    };
  }, [open]);

  useEffect(() => setActive(0), [query]);

  const go = (cmd: Command | undefined) => {
    if (!cmd) return;
    // 图编辑器脏态时拦下：Ctrl+K 是客户端跳转，不经过 handleBack。
    // 先关面板再走 —— 未保存对话框渲染在页面层，关掉浮层才能看到
    if (!navigationGuard.enter(cmd.to)) {
      onClose();
      return;
    }
    onClose();
    navigate(cmd.to, cmd.state === undefined ? undefined : { state: cmd.state });
  };

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      // 输入法组词期间这些键属于候选框：拼音打"模型"时按回车是上字，
      // 抢过来就直接跳页了；方向键同理是翻候选页。
      // keyCode 229 是部分输入法不置 isComposing 时的兜底信号。
      if (e.isComposing || e.keyCode === 229) return;

      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        setActive((a) => Math.min(a + 1, Math.max(results.length - 1, 0)));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setActive((a) => Math.max(a - 1, 0));
      } else if (e.key === "Enter") {
        e.preventDefault();
        go(results[active]);
      } else if (e.key === "Tab") {
        // 模态内没有第二个可聚焦元素，Tab 会漏到背后的页面上；
        // 选择本来就交给 ↑↓，这里把焦点钉在输入框
        e.preventDefault();
        inputRef.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, results, active, onClose, navigate]);

  // 键盘移动时把选中项滚进可视区
  useEffect(() => {
    listRef.current
      ?.querySelector<HTMLElement>('[data-selected="true"]')
      ?.scrollIntoView({ block: "nearest" });
  }, [active, results]);

  if (!open) return null;

  const selectedId = results[active]?.id;
  const groups: { title: string; cmds: Command[] }[] = [
    { title: "跳转", cmds: results.filter((c) => c.group === "跳转") },
    ...NAV_GROUPS.map((g) => ({
      title: g.title,
      cmds: results.filter((c) => c.group === g.title),
    })),
    {
      title: "操作",
      cmds: results.filter((c) => c.group === "操作"),
    },
  ].filter((g) => g.cmds.length > 0);
  const optionId = (cmd: Command) => `cmdk-opt-${cmd.id.replace(/[^\w-]/g, "_")}`;

  return (
    <div
      className="cmdk-backdrop"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="cmdk" role="dialog" aria-modal="true" aria-label="快速跳转">
        <div className="cmdk-input-row">
          <Search size={15} aria-hidden />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索页面，或粘贴 Trace / Loop ID…"
            aria-label="搜索页面或操作，也可粘贴 ID 直接跳转"
            spellCheck={false}
            role="combobox"
            aria-expanded
            aria-controls="cmdk-list"
            {...(selectedId
              ? { "aria-activedescendant": optionId(results[active] as Command) }
              : {})}
          />
          <span className="kbd">esc</span>
        </div>

        <div
          className="cmdk-list"
          id="cmdk-list"
          ref={listRef}
          role="listbox"
          aria-label="命令列表"
        >
          {groups.length === 0 ? (
            <p className="cmdk-empty">
              没有匹配「{query}」的命令。完整的 32 位十六进制或 UUID 会出现跳转入口。
            </p>
          ) : (
            groups.map((group) => (
              <div key={group.title} role="presentation">
                <p className="cmdk-group-title">{group.title}</p>
                {group.cmds.map((cmd) => (
                  <div
                    key={cmd.id}
                    id={optionId(cmd)}
                    role="option"
                    aria-selected={cmd.id === selectedId}
                    data-selected={cmd.id === selectedId}
                    className="cmdk-item"
                    onMouseEnter={() =>
                      setActive(results.findIndex((r) => r.id === cmd.id))
                    }
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => go(cmd)}
                  >
                    <cmd.icon size={15} aria-hidden />
                    <span>{cmd.label}</span>
                    {cmd.hint && <span className="cmdk-hint">{cmd.hint}</span>}
                  </div>
                ))}
              </div>
            ))
          )}
        </div>

        <footer className="cmdk-foot">
          <span>
            <span className="kbd">↑</span>
            <span className="kbd">↓</span> 选择
          </span>
          <span>
            <span className="kbd">↵</span> 打开
          </span>
          <span>
            <span className="kbd">esc</span> 关闭
          </span>
        </footer>
      </div>
    </div>
  );
}
