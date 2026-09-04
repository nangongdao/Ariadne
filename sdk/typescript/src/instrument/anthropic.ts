/**
 * Anthropic 自动埋点。
 *
 * 与 Python SDK 对等：包装 Anthropic SDK 的 messages.create 方法。
 *
 * 用法：
 *   import Anthropic from "@anthropic-ai/sdk";
 *   import { wrapAnthropic } from "@ariadne/sdk/instrument/anthropic";
 *   const client = wrapAnthropic(new Anthropic({ apiKey: "..." }));
 *   await client.messages.create({ model: "claude-3-opus", messages: [...] });
 */

import { getClient } from "../client";

const PROVIDER = "anthropic";

interface AnthropicRequest {
  model?: string;
  messages?: unknown;
  system?: string;
  max_tokens?: number;
  temperature?: number;
  stream?: boolean;
}

interface AnthropicResponse {
  model?: string;
  usage?: {
    input_tokens?: number;
    output_tokens?: number;
    cache_read_input_tokens?: number;
    cache_creation_input_tokens?: number;
  };
  content?: Array<{ text?: string }>;
}

function previewMessages(messages: unknown): string {
  if (!Array.isArray(messages)) return "";
  return messages
    .slice(0, 5)
    .map((m: { role?: string; content?: unknown }) => {
      const content = typeof m.content === "string" ? m.content : JSON.stringify(m.content);
      return `${m.role ?? "?"}: ${content.slice(0, 200)}`;
    })
    .join("\n")
    .slice(0, 1024);
}

function previewOutput(resp: AnthropicResponse): string {
  if (resp.content?.[0]?.text) return resp.content[0].text.slice(0, 1024);
  return "";
}

export function wrapAnthropic<T extends Record<string, unknown>>(client: T): T {
  const messages = (client as unknown as {
    messages?: { create?: (...a: unknown[]) => unknown };
  }).messages;

  if (messages && typeof messages.create === "function") {
    const original = messages.create;
    messages.create = function (...args: unknown[]): unknown {
      const request = (args[0] as AnthropicRequest) ?? {};
      const ariadne = getClient();
      if (!ariadne) return original.apply(messages, args);

      const model = request.model ?? "";
      const span = ariadne.span(`messages.create ${model}`.trim(), {
        kind: "llm",
        operation: "messages.create",
        provider: PROVIDER,
        model,
      });

      span.setAttributes({
        temperature: request.temperature,
        max_tokens: request.max_tokens,
        stream: request.stream ?? false,
      });

      if (request.messages) span.setInput(previewMessages(request.messages));
      if (request.system) {
        span.setAttributes({ system_prompt_length: request.system.length });
      }

      span.enter();

      const result = original.apply(messages, args);

      if (result instanceof Promise) {
        return result.then(
          (resp: unknown) => {
            const r = resp as AnthropicResponse;
            if (r?.model) span.setModel(model, { responseModel: r.model });
            if (r?.usage) {
              span.setUsage({
                input_tokens: r.usage.input_tokens ?? 0,
                output_tokens: r.usage.output_tokens ?? 0,
                cache_read_tokens: r.usage.cache_read_input_tokens ?? 0,
                cache_write_tokens: r.usage.cache_creation_input_tokens ?? 0,
                reasoning_tokens: 0,
              });
            }
            span.setOutput(previewOutput(r));
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

      const r = result as AnthropicResponse;
      if (r?.model) span.setModel(model, { responseModel: r.model });
      if (r?.usage) {
        span.setUsage({
          input_tokens: r.usage.input_tokens ?? 0,
          output_tokens: r.usage.output_tokens ?? 0,
          cache_read_tokens: r.usage.cache_read_input_tokens ?? 0,
          cache_write_tokens: r.usage.cache_creation_input_tokens ?? 0,
          reasoning_tokens: 0,
        });
      }
      span.setOutput(previewOutput(r));
      span.end();
      return result;
    };
  }

  return client;
}
