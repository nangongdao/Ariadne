/**
 * 工作目录路径校验 —— 必须与后端 _safe_workspace_paths 同规则。
 *
 * 两侧各挡一次：后端因为请求体不可信，前端因为不该让人填完文件内容
 * 才吃一个 422。规则漂移会让前端放行、后端拒绝，所以这些用例照抄了
 * 后端 test_loops_api.py 里的那组参数。
 */

import { describe, expect, it } from "vitest";

import { validateWorkspacePath } from "@/components/WorkspaceFilesField";

describe("validateWorkspacePath", () => {
  it("接受普通相对路径", () => {
    expect(validateWorkspacePath("solution.py")).toBeNull();
    expect(validateWorkspacePath("pkg/mod.py")).toBeNull();
    expect(validateWorkspacePath("a/b/c/test_x.py")).toBeNull();
  });

  it("拒绝空路径", () => {
    expect(validateWorkspacePath("")).toContain("不能为空");
    expect(validateWorkspacePath("   ")).toContain("不能为空");
  });

  it("拒绝绝对路径", () => {
    expect(validateWorkspacePath("/etc/passwd")).toContain("绝对路径");
  });

  it("拒绝目录穿越", () => {
    expect(validateWorkspacePath("../escaped.py")).toContain("穿越");
    expect(validateWorkspacePath("a/../../b.py")).toContain("穿越");
  });

  it("反斜杠形式的穿越同样拒绝", () => {
    // Windows 上用户会写反斜杠，规范化后必须走同一条判断
    expect(validateWorkspacePath("..\\escaped.py")).toContain("穿越");
  });

  it("拒绝盘符", () => {
    expect(validateWorkspacePath("C:\\Windows\\evil.py")).toContain("盘符");
  });

  it("不把文件名里的点号当成穿越", () => {
    // ".." 只有作为完整路径段时才是穿越，`..foo` 或 `a..b` 不是
    expect(validateWorkspacePath("test..py")).toBeNull();
    expect(validateWorkspacePath("a..b/c.py")).toBeNull();
  });
});
