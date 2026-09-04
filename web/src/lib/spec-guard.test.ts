/**
 * 锁定「固化出来的 spec 必然通过」这件事能被检出。
 *
 * 后端 playground.py:210-218 在无断言时注入 pattern="." / blocking=false 的占位断言。
 * 这份 spec 放进 CI 永远是绿的，界面上却只有一个复制按钮。
 * 这个不变量最容易悄悄烂掉：后端哪天改了占位断言的形状，检测就失效，
 * 而失效的表现是"警告不再出现"—— 没有测试根本发现不了。
 */

import { describe, expect, it } from "vitest";

import { inspectSpec } from "./spec-guard";

/** 后端占位断言的真实形状，与 playground.py:210-218 一致 */
const PLACEHOLDER_SPEC = {
  goal: {
    task: "demo",
    assertions: [
      {
        id: "placeholder",
        kind: "regex",
        spec: { pattern: "." },
        blocking: false,
        hint: "Playground 固化时的占位断言，请替换为真实断言",
      },
    ],
  },
};

describe("inspectSpec", () => {
  it("后端占位断言判为必然通过 —— 这是这个模块存在的唯一理由", () => {
    const result = inspectSpec(PLACEHOLDER_SPEC);
    expect(result.alwaysPasses).toBe(true);
    expect(result.flaws).toHaveLength(1);
    expect(result.flaws[0]?.id).toBe("placeholder");
  });

  it("真实断言不误报：误报会让人把有效断言当成没用的", () => {
    const result = inspectSpec({
      goal: {
        task: "demo",
        assertions: [
          { id: "has-json", kind: "regex", spec: { pattern: "^\\{" }, blocking: true },
        ],
      },
    });
    expect(result.alwaysPasses).toBe(false);
    expect(result.flaws).toEqual([]);
  });

  it("blocking 缺省按后端默认 true 处理，不能报成假绿", () => {
    const result = inspectSpec({
      goal: { assertions: [{ id: "a", kind: "contains", spec: { text: "ok" } }] },
    });
    expect(result.alwaysPasses).toBe(false);
    expect(result.flaws).toEqual([]);
  });

  it("空断言列表同样是必然通过", () => {
    const result = inspectSpec({ goal: { task: "demo", assertions: [] } });
    expect(result.alwaysPasses).toBe(true);
    expect(result.assertionCount).toBe(0);
  });

  it("有阻塞断言时，另一条非阻塞断言只记瑕疵，不判为假绿", () => {
    const result = inspectSpec({
      goal: {
        assertions: [
          { id: "real", kind: "contains", spec: { text: "ok" }, blocking: true },
          { id: "soft", kind: "contains", spec: { text: "nice" }, blocking: false },
        ],
      },
    });
    expect(result.alwaysPasses).toBe(false);
    expect(result.flaws.map((f) => f.id)).toEqual(["soft"]);
  });

  it("阻塞但万能的正则要报出来：它拦不住任何输出，却看着像在把关", () => {
    const result = inspectSpec({
      goal: { assertions: [{ id: "anything", kind: "regex", spec: { pattern: ".*" } }] },
    });
    expect(result.alwaysPasses).toBe(false);
    expect(result.flaws[0]?.reason).toContain("匹配任何输出");
  });

  it("只对 regex 判万能 pattern —— 别的 kind 里 pattern 语义未知", () => {
    const result = inspectSpec({
      goal: { assertions: [{ id: "j", kind: "json_path", spec: { pattern: "." } }] },
    });
    expect(result.flaws).toEqual([]);
  });

  it("形状不对时不炸也不误报 —— 后端返回结构变了不该让整页白屏", () => {
    for (const bad of [null, undefined, "yaml", 42, [], {}, { goal: null }]) {
      expect(() => inspectSpec(bad)).not.toThrow();
    }
    expect(inspectSpec({ goal: { assertions: "oops" } }).assertionCount).toBe(0);
  });

  it("缺 id 时给出可指认的位置，而不是空字符串", () => {
    const result = inspectSpec({
      goal: { assertions: [{ kind: "regex", spec: { pattern: "." } }] },
    });
    expect(result.flaws[0]?.id).toBe("#1");
  });
});
