"""RFC 9457 Problem Details 错误响应。

统一错误格式的价值在于客户端可以按 `type` 做程序化处理，
而不是解析人类可读的错误消息。
"""

from __future__ import annotations

from typing import Any

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

_BASE_URI = "https://ariadne.dev/errors"
CONTENT_TYPE = "application/problem+json"


class ApiError(Exception):
    """业务错误基类。子类通过类属性声明语义。"""

    error_type = "internal"
    title = "内部错误"
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR

    def __init__(self, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.detail = detail
        self.extra = extra

    def to_problem(self) -> dict[str, Any]:
        return {
            "type": f"{_BASE_URI}/{self.error_type}",
            "title": self.title,
            "status": self.status_code,
            "detail": self.detail,
            **self.extra,
        }


class UnauthorizedError(ApiError):
    error_type = "unauthorized"
    title = "认证失败"
    status_code = status.HTTP_401_UNAUTHORIZED


class UnknownFormatError(ApiError):
    error_type = "unknown-format"
    title = "未知的遥测格式"
    status_code = status.HTTP_400_BAD_REQUEST


class BatchTooLargeError(ApiError):
    error_type = "batch-too-large"
    title = "批次过大"
    status_code = status.HTTP_413_CONTENT_TOO_LARGE


class BadRequestError(ApiError):
    error_type = "bad-request"
    title = "请求参数无效"
    status_code = status.HTTP_400_BAD_REQUEST


class UnprocessableError(ApiError):
    """请求语义上不可处理（如目标不可验证）。

    与 400 的区别：400 是格式错误，422 是"格式对但语义上无法满足"
    —— 目标不可验证正是这种（docs/09 明确返回 422）。
    """

    error_type = "unverifiable-goal"
    title = "目标不可验证"
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT


class ConflictError(ApiError):
    error_type = "conflict"
    title = "资源冲突"
    status_code = status.HTTP_409_CONFLICT


class NotFoundError(ApiError):
    error_type = "not-found"
    title = "资源不存在"
    status_code = status.HTTP_404_NOT_FOUND


class UpstreamUnavailableError(ApiError):
    error_type = "upstream-unavailable"
    title = "依赖服务不可用"
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE


class ForbiddenError(ApiError):
    """权限不足。角色缺少所需权限。"""

    error_type = "forbidden"
    title = "权限不足"
    status_code = status.HTTP_403_FORBIDDEN


async def api_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return JSONResponse(
        status_code=exc.status_code, content=exc.to_problem(), media_type=CONTENT_TYPE
    )


async def validation_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={
            "type": f"{_BASE_URI}/validation",
            "title": "请求校验失败",
            "status": status.HTTP_422_UNPROCESSABLE_CONTENT,
            "detail": "请求体不符合 schema",
            "errors": [
                {"loc": list(e.get("loc", [])), "msg": e.get("msg", "")}
                for e in exc.errors()[:20]
            ],
        },
        media_type=CONTENT_TYPE,
    )
