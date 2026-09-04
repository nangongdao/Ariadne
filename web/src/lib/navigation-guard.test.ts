import { beforeEach, describe, expect, it, vi } from "vitest";

import { navigationGuard } from "@/lib/navigation-guard";

/**
 * 单例全局状态：每个用例前用「放行守卫」覆盖掉残留，保证隔离。
 * register 语义：后注册覆盖先注册；unregister 只清「自己仍是当前守卫」的情况。
 */
function resetGuard(): void {
  navigationGuard.register(() => true);
}

describe("navigationGuard", () => {
  beforeEach(resetGuard);

  it("默认放行（守卫不拦截时直接通过）", () => {
    expect(navigationGuard.enter("/traces")).toBe(true);
  });

  it("已在 nav 入口调用 enter 时把目的地交给守卫判断", () => {
    const fn = vi.fn((_to: string) => false);
    navigationGuard.register(fn);
    expect(navigationGuard.enter("/loops")).toBe(false);
    expect(fn).toHaveBeenCalledWith("/loops");
  });

  it("守卫返回 true 表示放行", () => {
    navigationGuard.register(() => true);
    expect(navigationGuard.enter("/anything")).toBe(true);
  });

  it("守卫返回 false 表示拦截", () => {
    navigationGuard.register(() => false);
    expect(navigationGuard.enter("/anything")).toBe(false);
  });

  it("unregister 后不再拦截", () => {
    const fn = vi.fn(() => false);
    const unregister = navigationGuard.register(fn);
    unregister();
    expect(navigationGuard.enter("/traces")).toBe(true);
    expect(fn).not.toHaveBeenCalled();
  });

  it("后注册的守卫覆盖先注册的", () => {
    const first = vi.fn(() => false);
    const second = vi.fn(() => true);
    navigationGuard.register(first);
    navigationGuard.register(second);
    expect(navigationGuard.enter("/graphs")).toBe(true);
    expect(second).toHaveBeenCalled();
    expect(first).not.toHaveBeenCalled();
  });

  it("注销被覆盖的守卫不会误清仍在位的守卫", () => {
    const first = vi.fn(() => true);
    const second = vi.fn(() => false);
    const unregisterFirst = navigationGuard.register(first);
    navigationGuard.register(second);
    // first 已被 second 覆盖，注销 first 不能清掉仍是当前守卫的 second
    unregisterFirst();
    expect(navigationGuard.enter("/x")).toBe(false);
    expect(second).toHaveBeenCalled();
  });
});