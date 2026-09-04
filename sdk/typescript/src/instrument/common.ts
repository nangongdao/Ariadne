/**
 * 自动埋点的共用工具函数。
 * 与 Python SDK 的 instrument/common.py 对等。
 */

interface OpenAIMessage {
  role?: string;
  content?: string | unknown;
}

interface OpenAIUsage {
  prompt_tokens?: number;
  completion_tokens?: number;
  cached_tokens?: number;
  reasoning_tokens?: number;
}

interface OpenAIResponse {
  model?: string;
  usage?: OpenAIUsage;
  choices?: Array<{ message?: OpenAIMessage }>;
  output?: string;
}

/** 从 messages 数组生成预览。 */
export function previewMessages(messages: OpenAIMessage[]): string {
  return messages
    .slice(0, 5)
    .map((m) => `${m.role ?? "?"}: ${String(m.content ?? "").slice(0, 200)}`)
    .join("\n")
    .slice(0, 1024);
}

/** 从 response 生成输出预览。 */
export function previewOutput(response: OpenAIResponse): string {
  if (response.choices?.[0]?.message?.content) {
    return String(response.choices[0].message.content).slice(0, 1024);
  }
  if (response.output) {
    return String(response.output).slice(0, 1024);
  }
  return "";
}

/** 从 OpenAI usage 提取 token 计数。 */
export function extractOpenAIUsage(usage: OpenAIUsage | undefined) {
  return {
    input_tokens: usage?.prompt_tokens ?? 0,
    output_tokens: usage?.completion_tokens ?? 0,
    cache_read_tokens: usage?.cached_tokens ?? 0,
    cache_write_tokens: 0,
    reasoning_tokens: usage?.reasoning_tokens ?? 0,
  };
}
