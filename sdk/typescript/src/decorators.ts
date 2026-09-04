/**
 * @trace 装饰器（TypeScript 版）。
 *
 * 与 Python SDK 对等：把函数调用记录为 span。未调用 init() 时是零开销直通。
 *
 * TS 没有 Python 那样的 inspect.iscoroutinefunction，但 async 函数自然返回
 * Promise，用同一个 wrapper 处理同步和异步调用。
 *
 * 用法：
 *   import { trace } from "@ariadne/sdk";
 *   class MyService {
 *     @trace("retrieve", { kind: "rag" })
 *     retrieve(query: string) { ... }
 *   }
 */

import { getClient } from "./client";
import { Span } from "./span";

const MAX_ARG_PREVIEW = 512;

function previewArgs(args: unknown[]): string {
  const parts = args.slice(0, 3).map((a) => {
    try {
      return String(a).slice(0, 120);
    } catch {
      return "[unrepr]";
    }
  });
  return parts.join(", ").slice(0, MAX_ARG_PREVIEW);
}

interface TraceOptions {
  kind?: string;
  captureIo?: boolean;
}

/** 包装一个函数，在其执行前后自动产生 span。 */
export function traced<T extends (...args: unknown[]) => unknown>(
  fn: T,
  name: string,
  opts: TraceOptions = {},
): T {
  const kind = opts.kind ?? "internal";
  const captureIo = opts.captureIo ?? true;

  const wrapped = function (this: unknown, ...args: unknown[]): unknown {
    const client = getClient();
    if (!client) return fn.apply(this, args);

    const span = client.span(name, { kind });
    if (captureIo) span.setInput(previewArgs(args));
    span.enter();

    try {
      const result = fn.apply(this, args);
      if (result instanceof Promise) {
        return result.then(
          (val) => {
            if (captureIo && val !== undefined && val !== null) {
              span.setOutput(String(val));
            }
            span.end();
            return val;
          },
          (err: Error) => {
            span.recordError(err);
            span.end();
            throw err;
          },
        );
      }
      if (captureIo && result !== undefined && result !== null) {
        span.setOutput(String(result));
      }
      span.end();
      return result;
    } catch (err) {
      span.recordError(err as Error);
      span.end();
      throw err;
    }
  } as T;
  return wrapped;
}

/** 方法装饰器工厂。与 Python 的 @trace(name, kind=...) 对等。 */
export function trace(
  name?: string,
  opts: TraceOptions = {},
): MethodDecorator {
  const kind = opts.kind ?? "internal";
  const captureIo = opts.captureIo ?? true;

  return function (
    _target: object,
    propertyKey: string | symbol,
    descriptor: PropertyDescriptor,
  ): PropertyDescriptor {
    if (!descriptor || typeof descriptor.value !== "function") {
      return descriptor;
    }
    const spanName = name ?? String(propertyKey);
    descriptor.value = traced(descriptor.value, spanName, { kind, captureIo });
    return descriptor;
  };
}

export { Span };
