"""P2-11：健康/统计/指标端点认证测试。"""

from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from ariadne.api.app import create_app
from ariadne.config import Settings


@pytest.fixture
def mock_settings_no_auth(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """require_meta_auth=False（默认）配置。"""
    monkeypatch.setenv("ARIADNE_API_REQUIRE_META_AUTH", "false")
    # 清除缓存，强制重新读取
    from ariadne.config import get_settings

    get_settings.cache_clear()
    settings = Settings()
    return settings


@pytest.fixture
def mock_settings_with_auth(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """require_meta_auth=True 配置。"""
    monkeypatch.setenv("ARIADNE_API_REQUIRE_META_AUTH", "true")
    from ariadne.config import get_settings

    get_settings.cache_clear()
    # 同时清除模块级缓存的 settings，确保 create_app 用新配置
    monkeypatch.setattr("ariadne.api.app.get_settings", get_settings)
    settings = Settings()
    return settings


@pytest.fixture
def mock_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    """模拟 app.state 依赖。"""
    # 不需要模拟 create_app，直接在测试中设置 state


class TestMetaEndpointsNoAuth:
    """require_meta_auth=False 时端点公开。"""

    def test_health_public_when_auth_disabled(
        self, mock_settings_no_auth: Settings
    ) -> None:
        """无认证时 /health 返回 200。"""
        app = create_app()
        app.state.store = Mock(ping=Mock(return_value=True))
        app.state.queue = Mock(ping=AsyncMock(return_value=True))
        app.state.pg = Mock(ping=AsyncMock(return_value=True))
        client = TestClient(app)

        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_stats_public_when_auth_disabled(
        self, mock_settings_no_auth: Settings
    ) -> None:
        """无认证时 /v1/stats 返回 200。"""
        app = create_app()
        app.state.queue = Mock(
            stream_length=AsyncMock(return_value=42),
            pending_count=AsyncMock(return_value=3),
        )
        client = TestClient(app)

        response = client.get("/v1/stats")
        assert response.status_code == 200
        assert response.json()["queue_length"] == 42

    def test_metrics_public_when_auth_disabled(
        self, mock_settings_no_auth: Settings
    ) -> None:
        """无认证时 /metrics 返回 200。"""
        app = create_app()
        client = TestClient(app)

        response = client.get("/metrics")
        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]


class TestMetaEndpointsWithAuth:
    """require_meta_auth=True 时端点要求 API Key。"""

    def test_health_requires_auth_when_enabled(
        self, mock_settings_with_auth: Settings
    ) -> None:
        """启用认证时 /health 无 key 返回 401。"""
        # create_app() 内部调用 get_settings()，需要传入 settings
        app = create_app(settings=mock_settings_with_auth)
        # 模拟依赖，避免端点内部访问 state 报错
        app.state.store = Mock(ping=Mock(return_value=True))
        app.state.queue = Mock(ping=AsyncMock(return_value=True))
        app.state.pg = Mock(ping=AsyncMock(return_value=True))
        client = TestClient(app)

        response = client.get("/health")
        assert response.status_code == 401

    def test_health_accepts_valid_key_when_auth_enabled(
        self, mock_settings_with_auth: Settings
    ) -> None:
        """启用认证时带正确 key 返回 200。"""
        app = create_app(settings=mock_settings_with_auth)
        app.state.store = Mock(ping=Mock(return_value=True))
        app.state.queue = Mock(ping=AsyncMock(return_value=True))
        app.state.pg = Mock(ping=AsyncMock(return_value=True))
        client = TestClient(app)

        key = mock_settings_with_auth.api.static_api_key.get_secret_value()
        response = client.get("/health", headers={"X-Ariadne-Key": key})
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_stats_requires_auth_when_enabled(
        self, mock_settings_with_auth: Settings
    ) -> None:
        """启用认证时 /v1/stats 无 key 返回 401。"""
        app = create_app(settings=mock_settings_with_auth)
        app.state.queue = Mock(
            stream_length=AsyncMock(return_value=42),
            pending_count=AsyncMock(return_value=3),
        )
        client = TestClient(app)

        response = client.get("/v1/stats")
        assert response.status_code == 401

    def test_metrics_requires_auth_when_enabled(
        self, mock_settings_with_auth: Settings
    ) -> None:
        """启用认证时 /metrics 无 key 返回 401。"""
        app = create_app(settings=mock_settings_with_auth)
        client = TestClient(app)

        response = client.get("/metrics")
        assert response.status_code == 401
