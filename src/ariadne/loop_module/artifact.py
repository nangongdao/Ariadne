"""LLM 产出物落盘 —— 把模型输出变成磁盘上的文件。

**这是代码生成闭环缺失的一环。** COMMAND 断言（pytest / ruff / tsc）
验证的是工作目录里的**文件**，而模型输出只是内存里的一个字符串。没有
这一层，两者永远对不上：每轮 pytest 跑的都是同一份没变过的文件，模型
哪怕第二轮就给出正确答案，断言也不会变绿 —— Loop 空转到 MAX_ITERATIONS。

（实测过：缺陷版 `add` + 第 2 轮给出正确实现的脚本模型，终态
MAX_ITERATIONS、磁盘文件仍是缺陷版。这就是 M3「代码生成场景闭环达标率」
一直没法验收的原因 —— 不是缺 API key，是这条路径根本不通。）

解析策略是**围栏代码块 + 路径标注**，因为这是模型在代码任务里的自然输出
形式，不需要额外的工具调用协议。支持四种标注写法（见 _extract_path），
认不出路径时落到 default_path，仍认不出就跳过并在报告里说明 —— 不猜。

安全边界：只防"写出工作目录"。**不防模型生成的代码本身做坏事** ——
pytest 会自动加载并执行 conftest.py，这是"跑模型生成的代码"的固有属性，
不是本模块能收敛的。见 restricted_exec 的模块 docstring 与
`exec.allow_untrusted_code`（默认 False）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol

from ariadne.utils.logging import get_logger

logger = get_logger(__name__)

# 单轮写入上限。模型偶尔会把整个仓库吐出来，或陷入重复生成 ——
# 没有上限时一轮就能写满磁盘。
MAX_FILES_PER_WRITE: Final = 32
MAX_BYTES_PER_FILE: Final = 1024 * 1024
MAX_TOTAL_BYTES: Final = 4 * 1024 * 1024

# 围栏代码块：``` 或 ~~~，允许缩进，捕获信息串与正文
_FENCE = re.compile(
    r"^(?P<indent>[ \t]{0,3})(?P<fence>`{3,}|~{3,})[ \t]*"
    r"(?P<info>[^\n]*)\n(?P<body>.*?)^(?P=indent)(?P=fence)[ \t]*$",
    re.MULTILINE | re.DOTALL,
)

# 信息串里的显式路径标注：```python path=src/foo.py / file=src/foo.py
_INFO_KV = re.compile(r"\b(?:path|file|filename)\s*=\s*['\"]?([^\s'\"]+)")

# 正文首行的注释标注：# file: src/foo.py  //  <!-- file: x -->
_BODY_COMMENT = re.compile(
    r"^[ \t]*(?:#|//|--|/\*|<!--)\s*(?:file|path|filename)\s*:\s*"
    r"['\"]?([^\s'\"*>]+)",
    re.IGNORECASE,
)

# 围栏前一行的 markdown 标题式路径：**src/foo.py**  `src/foo.py`  src/foo.py:
_PRECEDING = re.compile(
    r"^[ \t]*(?:[-*+]\s*)?(?:\*\*|`|__)?\s*"
    r"(?P<path>[\w.\-/\\]+\.[A-Za-z0-9_]{1,12})"
    r"\s*(?:\*\*|`|__)?\s*[:：]?\s*$"
)

# 信息串第二段像路径：```python src/foo.py
_INFO_BARE = re.compile(r"^[\w+#.\-]*\s+([\w.\-/\\]+\.[A-Za-z0-9_]{1,12})\s*$")


@dataclass(frozen=True)
class WriteReport:
    """落盘结果。供 critique 与日志消费。"""

    written: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    # 致命错误（路径越界、超限）。非空表示这轮产出物不可信
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def wrote_anything(self) -> bool:
        return bool(self.written)

    def describe(self) -> str:
        parts = []
        if self.written:
            parts.append(f"已写入 {len(self.written)} 个文件: {', '.join(self.written)}")
        if self.skipped:
            parts.append(f"跳过 {len(self.skipped)} 个无法定位路径的代码块")
        if self.error:
            parts.append(f"错误: {self.error}")
        return "；".join(parts) or "无产出物"


@dataclass(frozen=True)
class CodeBlock:
    """一个围栏代码块。path 为 None 表示没认出目标路径。"""

    body: str
    path: str | None = None
    language: str = ""


class ArtifactWriter(Protocol):
    """产出物落盘器。

    抽象成 Protocol：不同任务形态（单文件 / 多文件 / 补丁）解析方式不同，
    engine 只关心"输出被物化到工作目录了没有"。
    """

    def write(self, output: str, workdir: Path) -> WriteReport: ...


class NullArtifactWriter:
    """不落盘。用于纯文本生成场景（断言只看 output 字符串）。"""

    def write(self, output: str, workdir: Path) -> WriteReport:
        return WriteReport()


def parse_blocks(output: str) -> tuple[CodeBlock, ...]:
    """从模型输出里提取围栏代码块及其目标路径。"""
    blocks: list[CodeBlock] = []
    for match in _FENCE.finditer(output):
        info = match.group("info").strip()
        body = match.group("body")
        # 去掉围栏的公共缩进，否则写出去的 Python 会有多余前导空格
        indent = match.group("indent")
        if indent:
            body = "\n".join(
                line[len(indent) :] if line.startswith(indent) else line
                for line in body.split("\n")
            )
        preceding = output[: match.start()].rstrip("\n").rsplit("\n", 1)
        blocks.append(
            CodeBlock(
                body=body,
                path=_extract_path(info, body, preceding[-1] if preceding else ""),
                language=info.split()[0] if info.split() else "",
            )
        )
    return tuple(blocks)


def _extract_path(info: str, body: str, preceding_line: str) -> str | None:
    """按四种标注写法找目标路径。都认不出返回 None —— 不猜。

    顺序即优先级：显式 kv 最可靠，围栏前一行最弱（可能只是普通句子，
    因此 _PRECEDING 要求整行就是一个带扩展名的路径）。
    """
    if kv := _INFO_KV.search(info):
        return kv.group(1)
    if bare := _INFO_BARE.match(info):
        return bare.group(1)
    first_line = body.split("\n", 1)[0] if body else ""
    if comment := _BODY_COMMENT.match(first_line):
        return comment.group(1)
    if pre := _PRECEDING.match(preceding_line.strip()):
        return pre.group("path")
    return None


def _has_symlink_on_path(workdir: Path, candidate: Path) -> bool:
    """检查 workdir → candidate 路径链上是否存在符号链接。

    `_safe_target` 的 `.resolve()` 会跟随符号链接展开 —— 工作目录里若有一个
    指向外部的 symlink（模型上一轮生成的代码可用 `ln -s` 创建，或种子文件预置），
    写文件会跟着链接落到边界外。`is_symlink()` 用 lstat 语义不跟随，逐组件检查。
    candidate 已被拒绝绝对路径，必是相对 path。
    """
    current = workdir
    for part in candidate.parts:
        current = current / part
        try:
            if current.is_symlink():
                return True
        except OSError:
            # 无法检查（权限等）—— 保守拒绝
            return True
    return False


def _safe_target(workdir: Path, relative: str) -> Path | None:
    """解析目标路径，越界返回 None。

    绝对路径与 `../` 都要挡：模型输出不可信，一个 `../../.ssh/authorized_keys`
    就写到工作目录外面去了。路径链上任何符号链接同样拒绝（见
    `_has_symlink_on_path`）。
    """
    candidate = Path(relative.strip().strip("'\"").replace("\\", "/"))
    if candidate.is_absolute() or candidate.drive or candidate.root:
        return None
    if _has_symlink_on_path(workdir, candidate):
        logger.warning(
            "拒绝写入：路径链含符号链接",
            extra={"relative": relative},
        )
        return None
    target = (workdir / candidate).resolve()
    if not target.is_relative_to(workdir.resolve()):
        return None
    return target


def materialize(files: tuple[tuple[str, str], ...], workdir: Path) -> WriteReport:
    """把种子文件写进工作目录。路径穿越一律拒绝。

    与 FencedCodeWriter 共用 `_safe_target`：种子文件同样来自请求体，
    同样不可信 —— 一个 `../../.ssh/authorized_keys` 就写出去了。

    与 `restricted_exec.prepare_workspace` 的区别：那个是上下文管理器，
    退出即删，适合"跑一条命令"；Loop 的工作目录要跨轮存活，生命周期由
    Worker 按终态管理。
    """
    written: list[str] = []
    total = 0
    if len(files) > MAX_FILES_PER_WRITE:
        return WriteReport(error=f"种子文件数超过 {MAX_FILES_PER_WRITE} 个上限")
    for relative, content in files:
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_BYTES_PER_FILE:
            return WriteReport(
                written=tuple(written),
                error=f"种子文件 {relative!r} 超过单文件上限 {MAX_BYTES_PER_FILE} 字节",
            )
        total += len(encoded)
        if total > MAX_TOTAL_BYTES:
            return WriteReport(
                written=tuple(written),
                error=f"种子文件总量超过 {MAX_TOTAL_BYTES} 字节上限",
            )
        target = _safe_target(workdir, relative)
        if target is None:
            return WriteReport(
                written=tuple(written),
                error=f"种子文件路径越界，拒绝写入: {relative!r}",
            )
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            return WriteReport(
                written=tuple(written),
                error=f"写入种子文件 {relative} 失败: {type(exc).__name__}: {exc}",
            )
        written.append(relative)
    return WriteReport(written=tuple(written))


@dataclass
class FencedCodeWriter:
    """解析围栏代码块并写入工作目录。全量覆盖，不做增量补丁。

    全量覆盖是刻意的：补丁格式（unified diff / search-replace）省 token，
    但模型生成的补丁经常对不上上下文行，失败模式是"静默不生效"——
    那正是本模块要消灭的东西。宁可多花 token 也要让"写了什么"确定。
    """

    # 代码块没有路径标注时的落点。None 表示跳过（不猜）
    default_path: str | None = None
    # 只写这些语言的块。空集表示不限制
    languages: frozenset[str] = field(default_factory=frozenset)
    max_files: int = MAX_FILES_PER_WRITE
    max_bytes_per_file: int = MAX_BYTES_PER_FILE
    max_total_bytes: int = MAX_TOTAL_BYTES

    def write(self, output: str, workdir: Path) -> WriteReport:
        blocks = parse_blocks(output)
        if not blocks:
            return WriteReport(error="模型输出里没有围栏代码块，无产出物可验证")

        written: list[str] = []
        skipped: list[str] = []
        total = 0

        for index, block in enumerate(blocks):
            if self.languages and block.language.lower() not in self.languages:
                skipped.append(f"#{index}({block.language or '无语言标注'})")
                continue

            relative = block.path or self.default_path
            if not relative:
                skipped.append(f"#{index}(无路径标注)")
                continue

            encoded = block.body.encode("utf-8")
            if len(encoded) > self.max_bytes_per_file:
                return WriteReport(
                    written=tuple(written),
                    skipped=tuple(skipped),
                    error=f"{relative} 超过单文件上限 {self.max_bytes_per_file} 字节",
                )
            total += len(encoded)
            if total > self.max_total_bytes:
                return WriteReport(
                    written=tuple(written),
                    skipped=tuple(skipped),
                    error=f"单轮写入总量超过 {self.max_total_bytes} 字节上限",
                )
            if len(written) >= self.max_files:
                return WriteReport(
                    written=tuple(written),
                    skipped=tuple(skipped),
                    error=f"单轮写入文件数超过 {self.max_files} 个上限",
                )

            target = _safe_target(workdir, relative)
            if target is None:
                return WriteReport(
                    written=tuple(written),
                    skipped=tuple(skipped),
                    error=f"产出物路径越界，拒绝写入: {relative!r}",
                )

            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(block.body, encoding="utf-8")
            except OSError as exc:
                return WriteReport(
                    written=tuple(written),
                    skipped=tuple(skipped),
                    error=f"写入 {relative} 失败: {type(exc).__name__}: {exc}",
                )
            written.append(relative)

        if not written:
            return WriteReport(
                skipped=tuple(skipped),
                error="没有一个代码块能定位到目标路径（模型未标注文件名，也没有配置 default_path）",
            )

        logger.info(
            "产出物已落盘",
            extra={"files": written, "skipped": len(skipped)},
        )
        return WriteReport(written=tuple(written), skipped=tuple(skipped))


__all__ = [
    "ArtifactWriter",
    "CodeBlock",
    "FencedCodeWriter",
    "NullArtifactWriter",
    "WriteReport",
    "materialize",
    "parse_blocks",
]
