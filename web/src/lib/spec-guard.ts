/**
 * 判断固化出来的 spec 会不会「必然通过」。
 *
 * Playground 固化时不发送断言，后端就注入一条占位断言（playground.py:210-218）：
 * kind=regex、pattern="."、blocking=false。它既匹配任何非空输出，又不阻塞收敛 ——
 * 把这份 spec 直接放进 CI，结果永远是绿的，而它一个字都没验证。
 * 后端在 hint 里写了「请替换为真实断言」，但那行字埋在 YAML 中间，
 * 用户看到的是一个可以直接复制的代码块和一个复制按钮。
 *
 * 只认后端会生成、以及手写时最容易写出的那几个万能 pattern：通用地判定
 * 「正则是否匹配一切」不现实，这里宁可漏报也不误报 —— 误报会让真实断言
 * 被打上"没用"的标签，那比漏报更糟。
 */

/** 匹配任意输入（或任意非空输入）的 pattern。手写 spec 时最常见的几个 */
const ALWAYS_MATCH_PATTERNS: ReadonlySet<string> = new Set([
  ".",
  ".*",
  ".+",
  "^",
  "$",
  "^$",
  "^.*$",
  "^.+$",
  "(.*)",
  "[\\s\\S]*",
  "[\\s\\S]+",
]);

export interface AssertionFlaw {
  id: string;
  /** 为什么这条断言拦不住任何东西 */
  reason: string;
}

export interface SpecInspection {
  flaws: AssertionFlaw[];
  /**
   * 没有任何阻塞性断言 —— 无论输出是什么，这份 spec 的退出码都是 0。
   * 这是「CI 假绿」的判定条件，比单条断言有瑕疵严重得多。
   */
  alwaysPasses: boolean;
  assertionCount: number;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** 检查 spec_dict（后端 FreezeSpecResponse.spec_dict，形状见 spec_module/schema.py） */
export function inspectSpec(specDict: unknown): SpecInspection {
  const goal = isRecord(specDict) ? specDict["goal"] : undefined;
  const rawAssertions = isRecord(goal) ? goal["assertions"] : undefined;
  const assertions = Array.isArray(rawAssertions) ? rawAssertions : [];

  const flaws: AssertionFlaw[] = [];
  let blockingCount = 0;

  for (const [index, raw] of assertions.entries()) {
    if (!isRecord(raw)) continue;
    const id = typeof raw["id"] === "string" && raw["id"] ? raw["id"] : `#${index + 1}`;
    // blocking 缺失时按后端默认值 true 处理，否则会把正常 spec 报成假绿
    const blocking = raw["blocking"] !== false;
    if (blocking) blockingCount += 1;

    const spec = isRecord(raw["spec"]) ? raw["spec"] : {};
    const pattern = spec["pattern"];
    const trivialPattern =
      raw["kind"] === "regex" &&
      typeof pattern === "string" &&
      ALWAYS_MATCH_PATTERNS.has(pattern.trim());

    if (trivialPattern && !blocking) {
      flaws.push({ id, reason: `正则 ${JSON.stringify(pattern)} 匹配任何输出，且不阻塞` });
    } else if (trivialPattern) {
      flaws.push({ id, reason: `正则 ${JSON.stringify(pattern)} 匹配任何输出，等于没校验` });
    } else if (!blocking) {
      flaws.push({ id, reason: "blocking=false，失败也不会拦住流程" });
    }
  }

  return {
    flaws,
    // 一条断言都没有同样是「必然通过」，不能只看 blockingCount
    alwaysPasses: assertions.length === 0 || blockingCount === 0,
    assertionCount: assertions.length,
  };
}
