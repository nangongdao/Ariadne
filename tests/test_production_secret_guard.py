"""生产密钥校验的接线断言。

`Settings.env` 曾是个只写不读的字段：src 里没有任何分支读它，于是
`ARIADNE_ENV=production` 和 development 行为完全一致 —— 生产部署会带着出厂的
`change-me-in-production` 去签 JWT，任何拿到源码的人都能伪造管理员会话。

所以这里刻意不直接调 `validate_production_secrets`，而是从 `create_app()` 和
CLI 出发：直接调校验函数的测试证明的是"这个函数能判断对错"，而不是"启动时
真的会判断"。前者在函数没有任何调用点时照样全绿，那正是本仓库已经出现过
七次的失效方式。
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from ariadne.api.app import create_app
from ariadne.config import (
    ApiSettings,
    InsecureProductionConfigError,
    Settings,
    validate_production_secrets,
)

_STRONG = "x" * 32


def make_settings(
    *, env: str, jwt: str = _STRONG, api_key: str = "ak_real_production_key"
) -> Settings:
    """构造一份指定 env 与密钥的配置。

    api 段整体替换而不是改字段：配置模型是 frozen 的，这也是生产里唯一的
    合法构造方式（环境变量 → 模型），改字段的写法测不到校验的真实输入。
    """
    return Settings(
        env=env,
        api=ApiSettings(jwt_secret=SecretStr(jwt), static_api_key=SecretStr(api_key)),
    )


class TestCreateAppEnforcesSecrets:
    """真实启动路径：占位密钥必须让 API 起不来。"""

    def test_production_with_placeholder_jwt_secret_refuses_to_boot(self) -> None:
        settings = make_settings(env="production", jwt="change-me-in-production")
        with pytest.raises(InsecureProductionConfigError, match="jwt_secret"):
            create_app(settings)

    def test_production_with_placeholder_api_key_refuses_to_boot(self) -> None:
        settings = make_settings(env="production", api_key="ak_local_dev_key")
        with pytest.raises(InsecureProductionConfigError, match="static_api_key"):
            create_app(settings)

    def test_production_with_real_secrets_boots(self) -> None:
        app = create_app(make_settings(env="production"))
        assert app.title == "Ariadne API"

    def test_development_keeps_factory_defaults_usable(self) -> None:
        """本地开发不能被这条挡住 —— 否则大家会去改 env 而不是配密钥。"""
        settings = make_settings(env="development", jwt="change-me-in-production")
        assert create_app(settings).title == "Ariadne API"

    def test_error_message_names_the_env_var_to_set(self) -> None:
        """报错要能直接照着改，而不是只说"不安全"。"""
        settings = make_settings(env="production", jwt="change-me-in-production")
        with pytest.raises(InsecureProductionConfigError) as exc:
            create_app(settings)
        assert "ARIADNE_API_JWT_SECRET" in str(exc.value)


class TestHmacKeyLength:
    """HS256 用短密钥签名会被暴力破解，PyJWT 也会为此发 warning。"""

    def test_short_secret_rejected_in_production(self) -> None:
        with pytest.raises(InsecureProductionConfigError, match="至少需要 32 字节"):
            validate_production_secrets(make_settings(env="production", jwt="short"))

    def test_exactly_32_bytes_accepted(self) -> None:
        validate_production_secrets(make_settings(env="production", jwt="y" * 32))

    def test_multibyte_secret_measured_in_bytes_not_characters(self) -> None:
        """20 个汉字是 20 字符但 60 字节 —— 按字符数判会误拒。"""
        validate_production_secrets(make_settings(env="production", jwt="密钥" * 10))

    def test_prod_alias_is_also_enforced(self) -> None:
        """部署里写 prod 的比写 production 的多。"""
        with pytest.raises(InsecureProductionConfigError):
            validate_production_secrets(make_settings(env="prod", jwt="short"))

    def test_env_matching_is_case_insensitive(self) -> None:
        with pytest.raises(InsecureProductionConfigError):
            validate_production_secrets(make_settings(env="Production", jwt="short"))


class TestWorkerEntryIsGuardedToo:
    """Worker 不建 app，得单独接线 —— 否则这条只对 API 进程成立。"""

    def test_run_worker_validates_before_dispatch(self) -> None:
        import inspect

        from ariadne import cli

        source = inspect.getsource(cli.run_worker)
        assert "validate_production_secrets" in source

    def test_guard_runs_before_subcommand_branching(self) -> None:
        """校验必须在分支之前：放进某个 if 里就只覆盖一种 Worker。"""
        import inspect

        from ariadne import cli

        source = inspect.getsource(cli.run_worker)
        assert source.index("validate_production_secrets") < source.index(
            'if subcommand == "loop"'
        )
