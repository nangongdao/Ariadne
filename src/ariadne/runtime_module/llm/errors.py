"""LLM 适配器层异常 —— 类型化的 provider 错误。

429 单独建模：Retry-After 是结构化信息（重试决策的下限输入），
藏在 HTTPStatusError 的消息字符串里会导致调用方只能靠文本匹配识别。
"""

from __future__ import annotations

import httpx


class LLMRateLimitError(RuntimeError):
    """Provider 返回 429（速率限制）。

    retry_after 来自响应的 Retry-After 头（秒），可能为 None
    （provider 未提供时调用方退回指数退避）。
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def check_rate_limit(response: httpx.Response) -> None:
    """429 时抛 LLMRateLimitError（从 Retry-After 头解析秒数）。

    其余状态码不处理，交由调用方的 raise_for_status 走原有路径。
    Retry-After 可能是秒数或 HTTP-date；LLM provider 实际返回秒数，
    date 形式解析失败时退回 None（调用方用指数退避）。
    """
    if response.status_code != 429:
        return
    raw = response.headers.get("retry-after")
    retry_after: float | None = None
    if raw is not None:
        try:
            retry_after = float(raw)
        except ValueError:
            retry_after = None
    raise LLMRateLimitError("provider 速率限制 (HTTP 429)", retry_after=retry_after)


__all__ = ["LLMRateLimitError", "check_rate_limit"]
