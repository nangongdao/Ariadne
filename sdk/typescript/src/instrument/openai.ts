/**
 * OpenAI 自动埋点。
 *
 * 与 Python SDK 对等：显式包装公开方法，不全局 monkey patch 私有属性。
 * 后者在上游小版本升级时必碎，且会污染同进程内其他库的调用。
 *
 * 用法：
 *   import OpenAI from "openai";
 *   import { wrapOpenAI } from "@ariadne/sdk/instrument/openai";
 *   const client = wrapOpenAI(new OpenAI({ apiKey: "..." }));
 *   await client.chat.completions.create({ model: "gpt-4o", messages: [...] });
 */

import { getClient } from "../client";
import { extractOpenAIUsage, previewMessages, previewOutput } from "./common";

const PROVIDER = "openai";

interface OpenAIRequest {
  model?: string;
  messages?: unknown;
  input?: unknown;
  temperature?: number;
  max_tokens?: number;
  max_completion_tokens?: number;
  stream?: boolean;
}

/**
 * 包装 OpenAI 客户端的 chat.completions.create 方法。
 * 返回原客户端，透明地产生 span。
 */
export function wrapOpenAI<T extends Record<string, unknown>>(client: T): T {
  const completions = (client as unknown as {
    chat?: { completions?: { create?: (...a: unknown[]) => unknown } };
  }).chat?.completions;

  if (completions && typeof completions.create === "function") {
    const originalCreate = completions.create;
    completions.create = function (
      ...args: unknown[]
    ): unknown {
      const request = (args[0] as OpenAIRequest) ?? {};
      return _instrumentCreate(
        () => originalCreate.apply(completions, args),
        request,
        "chat.completions.create",
      );
    };
  }

  const embeddings = (client as unknown as {
    embeddings?: { create?: (...a: unknown[]) => unknown };
  }).embeddings;

  if (embeddings && typeof embeddings.create === "function") {
    const originalEmbed = embeddings.create;
    embeddings.create = function (
      ...args: unknown[]
    ): unknown {
      const request = (args[0] as OpenAIRequest) ?? {};
      return _instrumentCreate(
        () => originalEmbed.apply(embeddings, args),
        request,
        "embeddings.create",
      );
    };
  }

  return client;
}

function _instrumentCreate(
  invoke: () => unknown,
  request: OpenAIRequest,
  operation: string,
): unknown {
  const ariadne = getClient();
  if (!ariadne) return invoke();

  const model = request.model ?? "";
  const span = ariadne.span(`${operation} ${model}`.trim(), {
    kind: "llm",
    operation,
    provider: PROVIDER,
    model,
  });

  span.setAttributes({
    temperature: request.temperature,
    max_tokens: request.max_tokens ?? request.max_completion_tokens,
    stream: request.stream ?? false,
  });

  if (request.messages) {
    span.setInput(previewMessages(request.messages as never));
  } else if (request.input) {
    span.setInput(String(request.input).slice(0, 1024));
  }

  span.enter();

  const result = invoke();

  if (result instanceof Promise) {
    return result.then(
      (resp: unknown) => {
        const r = resp as { model?: string; usage?: unknown };
        if (r?.model) span.setModel(model, { responseModel: r.model });
        const usage = extractOpenAIUsage(r?.usage as never);
        span.setUsage(usage);
        span.setOutput(previewOutput(resp as never));
        span.end();
        return resp;
      },
      (err: Error) => {
        span.recordError(err);
        span.end();
        throw err;
      },
    );
  }

  const r = result as { model?: string; usage?: unknown };
  if (r?.model) span.setModel(model, { responseModel: r.model });
  span.setUsage(extractOpenAIUsage(r?.usage as never));
  span.setOutput(previewOutput(result as never));
  span.end();
  return result;
}
