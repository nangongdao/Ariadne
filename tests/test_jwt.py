"""JWT 会话令牌测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ariadne.auth.jwt import (
    SessionClaims,
    create_session_token,
    verify_session_token,
)
from ariadne.auth.rbac import Role
from ariadne.config import ApiSettings, Settings


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """覆盖全局 settings，提供确定性的 JWT secret。

    secret 补到 32 字节：PyJWT 会对更短的 HMAC 密钥发 InsecureKeyLengthWarning
    （RFC 7518 §3.2），此前这条 fixture 一个人贡献了全量套件的 21 条 warning。
    长度也对齐 config 里 _MIN_JWT_SECRET_BYTES 的生产下限 —— 测试用一个生产
    会拒绝的密钥，等于把校验规则本身排除在覆盖之外。
    """
    settings = Settings(
        env="test",
        api=ApiSettings(
            jwt_secret="test-secret-key-for-jwt-32bytes!",
            jwt_ttl_hours=1,
            jwt_issuer="test-ariadne",
        ),
    )
    monkeypatch.setattr("ariadne.auth.jwt.get_settings", lambda: settings)
    return settings


class TestCreateAndVerify:
    def test_create_returns_string(self) -> None:
        token = create_session_token("project-123", Role.ADMIN)
        assert isinstance(token, str)
        assert len(token) > 0

    def test_verify_returns_claims(self) -> None:
        token = create_session_token("project-123", Role.DEVELOPER)
        claims = verify_session_token(token)
        assert isinstance(claims, SessionClaims)
        assert claims.project_id == "project-123"
        assert claims.role == Role.DEVELOPER

    def test_verify_admin_role(self) -> None:
        token = create_session_token("p1", Role.ADMIN)
        claims = verify_session_token(token)
        assert claims.role == Role.ADMIN

    def test_verify_string_role(self) -> None:
        token = create_session_token("p1", "viewer")
        claims = verify_session_token(token)
        assert claims.role == Role.VIEWER

    def test_claims_has_expiry(self) -> None:
        token = create_session_token("p1", Role.VIEWER)
        claims = verify_session_token(token)
        assert claims.expires_at > datetime.now(UTC)

    def test_extra_claims_preserved(self, _settings: Settings) -> None:
        token = create_session_token("p1", Role.ADMIN, extra_claims={"org": "test-org"})
        import jwt

        api = _settings.api
        payload = jwt.decode(
            token, api.jwt_secret.get_secret_value(), algorithms=["HS256"]
        )
        assert payload["org"] == "test-org"


class TestInvalidTokens:
    def test_expired_token_rejected(self, _settings: Settings) -> None:
        """构造已过期的 token。"""
        import jwt

        # 用 fixture 返回的 settings，不要再调 get_settings()：fixture 只
        # monkeypatch 了 ariadne.auth.jwt 里的那个引用，这里直接调会拿到真实
        # 的生产默认值，于是签名密钥和被测代码用的密钥其实是两个不同的东西
        # —— 这条测试恰好因为"两边都错成同一个默认值"而通过。
        api = _settings.api
        now = datetime.now(UTC)
        payload = {
            "iss": api.jwt_issuer,
            "sub": "p1",
            "role": "admin",
            "iat": now - timedelta(hours=2),
            "exp": now - timedelta(hours=1),  # 1 小时前过期
        }
        token = jwt.encode(payload, api.jwt_secret.get_secret_value(), algorithm="HS256")
        with pytest.raises(ValueError, match="无效的会话令牌"):
            verify_session_token(token)

    def test_tampered_signature_rejected(self) -> None:
        """篡改签名首字符 —— 不能改末位。

        HS256 签名是 32 字节，base64url 编码成 43 个字符：末位字符只承载 2 个
        有效 bit，64 个字母表字符落进 16 个等价类，改末位有 1/16 概率解码出
        完全相同的签名字节。原实现改的正是末位，于是这条安全断言约 6% 的运行
        里假通过（实测 129/2000），且失败时看起来像"篡改未被检出"。
        首字符承载完整 6 bit，改它必然改变签名字节。
        """
        token = create_session_token("p1", Role.ADMIN)
        header, payload, signature = token.split(".")
        flipped = ("B" if signature[0] != "B" else "C") + signature[1:]
        tampered = f"{header}.{payload}.{flipped}"
        assert tampered != token
        with pytest.raises(ValueError, match="无效的会话令牌"):
            verify_session_token(tampered)

    def test_tampered_payload_rejected(self) -> None:
        """更贴近真实威胁：改 claims 提权，签名对不上必须拒。"""
        import base64
        import json

        token = create_session_token("p1", Role.VIEWER)
        header, payload, signature = token.split(".")
        claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
        claims["role"] = "admin"
        forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
        with pytest.raises(ValueError, match="无效的会话令牌"):
            verify_session_token(f"{header}.{forged}.{signature}")

    def test_wrong_secret_rejected(self) -> None:
        import jwt

        now = datetime.now(UTC)
        payload = {
            "iss": "test-ariadne",
            "sub": "p1",
            "role": "admin",
            "iat": now,
            "exp": now + timedelta(hours=1),
        }
        # 同样补到 32 字节：这条测的是"密钥不匹配"，不是"密钥太短"，用短密钥
        # 会混入一条与断言无关的 warning。
        token = jwt.encode(payload, "wrong-secret-but-also-32-bytes!!", algorithm="HS256")
        with pytest.raises(ValueError, match="无效的会话令牌"):
            verify_session_token(token)

    def test_missing_claims_rejected(self, _settings: Settings) -> None:
        """缺少必填声明的 token 被拒绝。"""
        import jwt

        api = _settings.api
        now = datetime.now(UTC)
        payload = {
            "iss": api.jwt_issuer,
            "sub": "p1",
            # 缺少 role
            "iat": now,
            "exp": now + timedelta(hours=1),
        }
        token = jwt.encode(payload, api.jwt_secret.get_secret_value(), algorithm="HS256")
        with pytest.raises(ValueError, match="无效的会话令牌"):
            verify_session_token(token)
