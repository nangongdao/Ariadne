"""产出物落盘的解析与安全边界测试。

`artifact.py` 是代码生成闭环里此前完全缺失的一环，因此这里既要覆盖
"能认出模型的各种输出写法"，也要覆盖"认不出时不猜"和"不许写出工作
目录"。闭环层面的验收在 `test_loop_benchmark.py`。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ariadne.loop_module.artifact import (
    FencedCodeWriter,
    NullArtifactWriter,
    parse_blocks,
)


class TestPathExtraction:
    """四种路径标注写法都要认得 —— 模型不会固定用某一种。"""

    def test_info_string_kv(self) -> None:
        blocks = parse_blocks("```python path=src/foo.py\nx = 1\n```")
        assert blocks[0].path == "src/foo.py"

    def test_info_string_file_kv(self) -> None:
        blocks = parse_blocks("```python file=a/b.py\nx = 1\n```")
        assert blocks[0].path == "a/b.py"

    def test_info_string_bare_path(self) -> None:
        blocks = parse_blocks("```python solution.py\nx = 1\n```")
        assert blocks[0].path == "solution.py"

    def test_body_comment(self) -> None:
        blocks = parse_blocks("```python\n# file: solution.py\nx = 1\n```")
        assert blocks[0].path == "solution.py"

    def test_preceding_line(self) -> None:
        blocks = parse_blocks("**solution.py**\n\n```python\nx = 1\n```")
        assert blocks[0].path == "solution.py"

    def test_preceding_backticks(self) -> None:
        blocks = parse_blocks("`app/main.py`:\n\n```python\nx = 1\n```")
        assert blocks[0].path == "app/main.py"

    def test_unlabelled_block_has_no_path(self) -> None:
        """认不出就是 None —— 不猜。猜错会覆盖用户的其他文件。"""
        blocks = parse_blocks("这是一段说明\n\n```python\nx = 1\n```")
        assert blocks[0].path is None

    def test_prose_before_block_is_not_a_path(self) -> None:
        """围栏前一行是普通句子时不能误当路径。"""
        blocks = parse_blocks("下面是修好的实现，请查收\n```python\nx = 1\n```")
        assert blocks[0].path is None


class TestBlockParsing:
    def test_multiple_blocks(self) -> None:
        text = (
            "```python path=a.py\nA = 1\n```\n\n"
            "```python path=b.py\nB = 2\n```\n"
        )
        blocks = parse_blocks(text)
        assert [b.path for b in blocks] == ["a.py", "b.py"]
        assert blocks[0].body == "A = 1\n"

    def test_tilde_fence(self) -> None:
        blocks = parse_blocks("~~~python path=a.py\nX = 1\n~~~")
        assert blocks[0].path == "a.py"

    def test_indented_fence_strips_common_indent(self) -> None:
        """列表里的代码块带缩进，原样写出去会是语法错误的 Python。"""
        blocks = parse_blocks("  ```python path=a.py\n  def f():\n      return 1\n  ```")
        assert blocks[0].body == "def f():\n    return 1\n"

    def test_no_blocks(self) -> None:
        assert parse_blocks("完全没有代码块的一段话") == ()


class TestWriting:
    def test_writes_labelled_block(self, tmp_path: Path) -> None:
        writer = FencedCodeWriter()
        report = writer.write("```python path=solution.py\nX = 1\n```", tmp_path)
        assert report.ok
        assert report.written == ("solution.py",)
        assert (tmp_path / "solution.py").read_text(encoding="utf-8") == "X = 1\n"

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        writer = FencedCodeWriter()
        report = writer.write("```python path=pkg/mod.py\nX = 1\n```", tmp_path)
        assert report.ok
        assert (tmp_path / "pkg" / "mod.py").is_file()

    def test_overwrites_existing_file(self, tmp_path: Path) -> None:
        """全量覆盖是刻意的策略，不是缺陷 —— 见 FencedCodeWriter 的 docstring。"""
        (tmp_path / "solution.py").write_text("OLD = 1\n", encoding="utf-8")
        FencedCodeWriter().write("```python path=solution.py\nNEW = 1\n```", tmp_path)
        assert (tmp_path / "solution.py").read_text(encoding="utf-8") == "NEW = 1\n"

    def test_default_path_used_for_unlabelled(self, tmp_path: Path) -> None:
        writer = FencedCodeWriter(default_path="main.py")
        report = writer.write("```python\nX = 1\n```", tmp_path)
        assert report.written == ("main.py",)

    def test_unlabelled_without_default_is_an_error(self, tmp_path: Path) -> None:
        """没有落点就报错，而不是静默丢弃 —— 静默丢弃会让 Loop 空转。"""
        report = FencedCodeWriter().write("```python\nX = 1\n```", tmp_path)
        assert not report.ok
        assert "定位" in report.error

    def test_no_code_block_is_an_error(self, tmp_path: Path) -> None:
        report = FencedCodeWriter().write("我觉得已经没问题了", tmp_path)
        assert not report.ok
        assert "围栏代码块" in report.error

    def test_language_filter(self, tmp_path: Path) -> None:
        writer = FencedCodeWriter(languages=frozenset({"python"}))
        report = writer.write(
            "```bash path=x.sh\nrm -rf /\n```\n```python path=a.py\nX=1\n```",
            tmp_path,
        )
        assert report.written == ("a.py",)
        assert not (tmp_path / "x.sh").exists()


class TestPathTraversalIsBlocked:
    """模型输出不可信：一个 ../ 就写到工作目录外面去了。"""

    @pytest.mark.parametrize(
        "path",
        [
            "../escaped.py",
            "../../etc/passwd",
            "sub/../../escaped.py",
        ],
    )
    def test_parent_traversal_rejected(self, tmp_path: Path, path: str) -> None:
        workdir = tmp_path / "work"
        workdir.mkdir()
        report = FencedCodeWriter().write(
            f"```python path={path}\nEVIL = 1\n```", workdir
        )
        assert not report.ok
        assert "越界" in report.error
        assert not (tmp_path / "escaped.py").exists()

    def test_absolute_path_rejected(self, tmp_path: Path) -> None:
        workdir = tmp_path / "work"
        workdir.mkdir()
        report = FencedCodeWriter().write(
            "```python path=/tmp/evil.py\nEVIL = 1\n```", workdir
        )
        assert not report.ok
        assert "越界" in report.error

    def test_windows_drive_path_rejected(self, tmp_path: Path) -> None:
        workdir = tmp_path / "work"
        workdir.mkdir()
        report = FencedCodeWriter().write(
            "```python path=C:\\Windows\\evil.py\nEVIL = 1\n```", workdir
        )
        assert not report.ok
        assert "越界" in report.error


class TestResourceLimits:
    """模型偶尔会陷入重复生成 —— 没有上限时一轮就能写满磁盘。"""

    def test_oversized_file_rejected(self, tmp_path: Path) -> None:
        writer = FencedCodeWriter(max_bytes_per_file=64)
        body = "X" * 200
        report = writer.write(f"```python path=a.py\n{body}\n```", tmp_path)
        assert not report.ok
        assert "单文件上限" in report.error

    def test_too_many_files_rejected(self, tmp_path: Path) -> None:
        writer = FencedCodeWriter(max_files=2)
        blocks = "".join(
            f"```python path=f{i}.py\nX = {i}\n```\n" for i in range(5)
        )
        report = writer.write(blocks, tmp_path)
        assert not report.ok
        assert "文件数" in report.error

    def test_total_size_rejected(self, tmp_path: Path) -> None:
        writer = FencedCodeWriter(max_total_bytes=100)
        blocks = "".join(
            f"```python path=f{i}.py\n{'X' * 60}\n```\n" for i in range(4)
        )
        report = writer.write(blocks, tmp_path)
        assert not report.ok
        assert "总量" in report.error


class TestNullWriter:
    def test_writes_nothing(self, tmp_path: Path) -> None:
        report = NullArtifactWriter().write("```python path=a.py\nX=1\n```", tmp_path)
        assert report.ok
        assert not report.written
        assert not (tmp_path / "a.py").exists()
