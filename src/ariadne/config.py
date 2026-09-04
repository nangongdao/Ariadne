"""集中配置。所有超参从环境变量或 .env 读取，禁止散落硬编码。"""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import quote
from uuid import UUID

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class _EnvFileSettings(BaseSettings):
    """所有配置段的共同基类，唯一职责是声明 .env 这个来源。

    env_file 必须逐个模型声明，不能只写在顶层 Settings 上：下面的字段都是
    default_factory 构造的独立 BaseSettings 实例，各自跑自己的来源链，顶层的
    env_file 对它们没有任何作用。曾经的表现是整个 .env 静默失效 —— 9 个键全部
    回落默认值，ARIADNE_PG_PORT=5433 被忽略后连到本机另一台 PG，报的却是
    WinError 64 连接重置，看不出是配置没读到。

    extra="ignore" 是必需的：每个模型都会读到整份 .env，其中带别的前缀的键
    对它而言都是多余字段，默认的 forbid 会直接抛 ValidationError。
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", frozen=True, extra="ignore"
    )


class ClickHouseSettings(_EnvFileSettings):
    model_config = SettingsConfigDict(env_prefix="ARIADNE_CH_")

    host: str = "localhost"
    port: int = 8123
    database: str = "ariadne"
    user: str = "default"
    password: SecretStr = SecretStr("")
    # 批量写触发条件：任一满足即 flush
    batch_max_rows: int = 5000
    batch_max_interval_ms: int = 200


class PostgresSettings(_EnvFileSettings):
    """事务型数据（dataset / experiment / prompt）。M2 起引入。

    两套凭据，刻意分开：
    - user / password         owner，有 DDL 权限，只给 Alembic 迁移用
    - app_user / app_password 非 owner，应用（API / Worker）连接用

    owner 会静默绕过 RLS（除非 FORCE ROW LEVEL SECURITY，而那会让迁移
    自己也被策略挡住）。应用拿 owner 连接时，13 张表上的 tenant_isolation
    策略等于没写。app_user 留空则回退 owner —— 本地开发和测试不必先建角色。
    """

    model_config = SettingsConfigDict(env_prefix="ARIADNE_PG_")

    host: str = "localhost"
    port: int = 5432
    database: str = "ariadne"
    user: str = "ariadne"
    password: SecretStr = SecretStr("ariadne")
    app_user: str = ""
    app_password: SecretStr = SecretStr("")
    pool_size: int = 10
    max_overflow: int = 5
    echo_sql: bool = False
    # 测试时指向 sqlite+aiosqlite 可跳过容器依赖
    dsn_override: str = ""

    def _dsn_for(self, user: str, password: str) -> str:
        # 凭据含 @ : / 等字符时必须百分号转义，否则 URL 解析会在该处截断，
        # 表现为"密码错误"而非"配置写错"，很难排查
        return (
            f"postgresql+asyncpg://{quote(user, safe='')}:{quote(password, safe='')}"
            f"@{self.host}:{self.port}/{self.database}"
        )

    def dsn(self) -> str:
        """owner 连接串 —— 迁移专用。"""
        if self.dsn_override:
            return self.dsn_override
        return self._dsn_for(self.user, self.password.get_secret_value())

    def app_dsn(self) -> str:
        """应用连接串 —— 走非 owner 角色，让 RLS 真正生效。

        app_user 未配置时回退 owner。此时 RLS 形同虚设，靠启动自检
        （ariadne.auth.rls）报警，而不是在这里静默失败。
        """
        if self.dsn_override:
            return self.dsn_override
        if not self.app_user:
            return self.dsn()
        return self._dsn_for(self.app_user, self.app_password.get_secret_value())


class RedisSettings(_EnvFileSettings):
    model_config = SettingsConfigDict(env_prefix="ARIADNE_REDIS_")

    url: str = "redis://localhost:6379/0"
    # 单独给密码留字段：K8s Secret 通常只存密码，不存整条 URL
    password: SecretStr = SecretStr("")
    stream_key: str = "q:collect"
    consumer_group: str = "collector-workers"
    # 消费者持有消息的可见性超时，超时后由 XAUTOCLAIM 回收
    visibility_timeout_ms: int = 90_000
    max_stream_length: int = 1_000_000
    # Loop 创建/恢复时的投递重试。最终失败由 API 响应显式标记，
    # 方便客户端稍后调用 resume，而不是把消息丢失伪装成 202 成功。
    enqueue_attempts: int = Field(default=3, ge=1, le=10)
    enqueue_backoff_ms: int = Field(default=250, ge=0, le=30_000)

    @model_validator(mode="after")
    def _fold_password_into_url(self) -> RedisSettings:
        """把 password 折进 url，让所有 from_url 调用点自动带上认证。

        六处 aioredis.from_url 都只读 .url。与其逐个改签名，不如在配置层
        把 URL 修正好 —— 漏改一处的表现是运行时 NOAUTH，且只在配了密码的
        环境才出现。

        url 里已含凭据时不覆盖：显式写法优先。
        """
        secret = self.password.get_secret_value()
        if not secret or "//" not in self.url:
            return self
        scheme, rest = self.url.split("//", 1)
        if "@" in rest:
            return self
        folded = f"{scheme}//:{quote(secret, safe='')}@{rest}"
        object.__setattr__(self, "url", folded)
        return self


class PayloadSettings(_EnvFileSettings):
    """大 payload 分级阈值。"""

    model_config = SettingsConfigDict(env_prefix="ARIADNE_PAYLOAD_")

    inline_max_bytes: int = 8 * 1024
    compress_max_bytes: int = 32 * 1024
    preview_chars: int = 512
    store_backend: str = "local"  # local | s3
    local_dir: str = "./data/payloads"
    s3_bucket: str = ""
    s3_prefix: str = "payloads"


class TelemetrySettings(_EnvFileSettings):
    model_config = SettingsConfigDict(env_prefix="ARIADNE_TELEMETRY_")

    # 显式锁定 semconv 版本：升级是一次带迁移的显式操作，不随依赖漂移
    genai_semconv_version: str = "1.37.0"
    redaction_enabled: bool = True
    # 是否保留 payload 全文。True = 分级存储（≤8KB 内联，8-32KB 压缩，>32KB 外溢），
    # False = 数据最小化（超 inline_max_bytes 即截断为 preview_chars 长度，全文不可恢复）。
    # 默认 True 以保持文档描述的分级行为（docs/06 §4.2）；置 False 需理解权衡。
    store_full_payload: bool = True


class ApiSettings(_EnvFileSettings):
    model_config = SettingsConfigDict(env_prefix="ARIADNE_API_")

    host: str = "0.0.0.0"
    port: int = 8000
    # 认证后端：static（M1 单 key）| db（M6 Argon2id + RBAC）
    auth_backend: str = "static"
    # M1 简化租户模型：单 project + 静态 key。完整 RBAC 见 M6。
    default_project_id: UUID = UUID("00000000-0000-0000-0000-000000000001")
    static_api_key: SecretStr = SecretStr("ak_local_dev_key")
    max_batch_spans: int = 1000
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    # JWT（Web Console 会话）
    jwt_secret: SecretStr = SecretStr("change-me-in-production")
    # 密钥轮换期的历史 jwt_secret（逗号分隔）。api_key 加密密钥由 jwt_secret
    # 派生：换主密钥后，旧密文靠这份清单继续可解（见 model_configs 解密
    # 顺序），重新保存过的配置即用新密钥重加密，全部迁完后清空本清单。
    previous_jwt_secrets: list[str] = Field(default_factory=list)
    jwt_ttl_hours: int = 24
    jwt_issuer: str = "ariadne"
    # Argon2id 参数
    argon2_memory_cost: int = 65536  # 64 MiB
    argon2_time_cost: int = 3
    argon2_parallelism: int = 1
    # 健康/统计/指标端点认证（P2-11）
    # 默认 False：Prometheus 抓取通常不带认证，反向代理做网络隔离
    # 置 True 时 /health、/v1/stats、/metrics 要求 API Key
    require_meta_auth: bool = False


class LlmSettings(_EnvFileSettings):
    """Loop Engine 的 LLM provider 配置。

    API key 只从环境变量/.env 读取（安全规则：绝无硬编码）。
    base_url 默认官方端点，可按需指向代理/网关。
    """

    model_config = SettingsConfigDict(env_prefix="ARIADNE_LLM_")

    provider: str = "anthropic"  # anthropic | openai
    model: str = "claude-sonnet-5"
    # 预算降级时用的便宜模型
    degraded_model: str = "claude-haiku-4-5"
    api_key: SecretStr = SecretStr("")
    base_url: str = "https://api.anthropic.com"


class HarnessSettings(_EnvFileSettings):
    """Harness 规则引擎配置（M4）。"""

    model_config = SettingsConfigDict(env_prefix="ARIADNE_HARNESS_")

    # 内置规则目录路径。空则不加载内置规则（仅 spec 驱动的规则生效）
    rules_dir: str = ""
    # CEL 求值超时（毫秒）
    eval_timeout_ms: int = 100
    # 是否启用审计日志
    audit_enabled: bool = True
    # fail-closed：规则求值失败时视为命中（拒绝）
    fail_closed: bool = True


class SandboxSettings(_EnvFileSettings):
    """沙箱配置（M4）。"""

    model_config = SettingsConfigDict(env_prefix="ARIADNE_SANDBOX_")

    # 沙箱 profile：strict / standard / trusted
    profile: str = "strict"
    # 预热池大小
    pool_size: int = 4
    # 是否允许不受信代码执行
    allow_untrusted_code: bool = False
    # gVisor 不可用时降级到受限子进程（M3 兼容）
    fallback_to_restricted: bool = True
    # 运行时镜像
    image: str = "ariadne/runtime-python:3.11"
    # Loop 工作目录的根。空字符串表示用系统临时目录。
    #
    # COMMAND 断言验证的是**磁盘上的文件**，没有工作目录时 CommandVerifier
    # 只能记 errored，而 goal_validation 会直接拒掉带 COMMAND 断言的目标
    # —— 也就是代码生成场景（M3 标杆场景）在生产路径上整个不可用。
    #
    # 按 loop_id 建子目录而非临时目录，是为了让同机接管的 Worker 能复用
    # 上一个 Worker 已落盘的产出物。
    workspace_root: str = ""


class ObservabilitySettings(_EnvFileSettings):
    """可观测性配置（M6）：Prometheus / SLO / 告警。"""

    model_config = SettingsConfigDict(env_prefix="ARIADNE_OBSERVABILITY_")

    # Prometheus HTTP API 端点。空字符串 → SLO 监控不启动（本地开发/测试）
    prometheus_url: str = ""
    # SLO 评估间隔（秒）
    slo_interval_seconds: float = 60.0


class WorkerSettings(_EnvFileSettings):
    """Worker 执行配置。"""

    model_config = SettingsConfigDict(env_prefix="ARIADNE_WORKER_")

    # 单个 Loop Worker 进程内并发执行几个 Loop。
    #
    # 必须 > 1：队列一次认领 _MAX_CLAIM=3 个任务，而单个 Loop 要跑几分钟。
    # 串行处理时后面的任务排在队头之后干等，idle 超过 RECLAIM_MIN_IDLE_MS
    # （90s）就会被其他 Worker 回收 —— 于是同一个 Loop 被两个 Worker 同时
    # 执行。并发度至少要跟上一次认领的批量。
    #
    # 上限不宜过大：每个 Loop 各自持有 LLM 连接、沙箱实例与工作目录。
    loop_concurrency: int = 3

    # 单个 Eval Worker 进程内并发执行几个实验。同上：eval_queue 一次认领
    # _MAX_CLAIM=5 个，回收阈值 120s，而一个实验要跑完整个数据集。
    # Eval 侧没有 Postgres 租约，判重只看 experiment.status，而 running
    # 不会让接管者跳过 —— 串行排队等于让整个实验被重复评测一遍。
    eval_concurrency: int = 5

    # 补偿扫描间隔（秒）。0 关闭扫描。
    #
    # Redis 入队失败（创建/审批/恢复时）会让 Loop 停在非终态但不在任何
    # 队列里 —— XREAD 恢复不了从未写入的消息。此扫描周期性把"非终态 +
    # 无有效租约 + 超过 idle 阈值无状态变化"的 Loop 重新入队，双重执行
    # 由 begin_lease 的条件更新挡住。见 loop_worker._reconcile。
    reconcile_interval_seconds: float = 60.0

    # 补偿扫描判定"卡住"的无进展阈值（秒）。必须大于正常认领延迟
    # （轮询 2s + 入队重试），否则刚入队还没被认领的任务会被重复入队
    # （重复无害但浪费）；也不必大到 15 分钟 —— 入队失败的 Loop 晚
    # 几分钟被执行是可接受的代价。
    reconcile_idle_seconds: int = 300

    # ---- Graph Worker（阶段 2/3）----

    # Graph 租约时长（秒）。Graph 执行通常较快（分钟级），但复杂图可能
    # 需要更长时间 —— 租约太短会让其他 Worker 在任务仍在执行时回收它。
    graph_lease_duration_s: int = 300

    # Graph 租约续期间隔（秒）。留足缓冲避免租约在长任务执行中过期。
    graph_lease_extend_interval_s: int = 120


class Settings(_EnvFileSettings):
    # 顶层两个字段也要前缀：不加时找的是裸 ENV / LOG_LEVEL，而 .env、compose、
    # Helm 写的都是 ARIADNE_ENV / ARIADNE_LOG_LEVEL，此前一直落在默认值上。
    # 下面那些嵌套段不受这个前缀影响：它们由 default_factory 构造，走自己的前缀。
    model_config = SettingsConfigDict(env_prefix="ARIADNE_")

    env: str = "development"
    log_level: str = "INFO"

    clickhouse: ClickHouseSettings = Field(default_factory=ClickHouseSettings)
    postgres: PostgresSettings = Field(default_factory=PostgresSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    payload: PayloadSettings = Field(default_factory=PayloadSettings)
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    llm: LlmSettings = Field(default_factory=LlmSettings)
    harness: HarnessSettings = Field(default_factory=HarnessSettings)
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    observability: ObservabilitySettings = Field(
        default_factory=ObservabilitySettings
    )
    worker: WorkerSettings = Field(default_factory=WorkerSettings)


#: 出厂占位值。生产环境用这些值启动等于没有认证 —— jwt_secret 是公开常量，
#: 任何人都能签出合法会话 token；static_api_key 同理。
_PLACEHOLDER_SECRETS: dict[str, str] = {
    "api.jwt_secret": "change-me-in-production",
    "api.static_api_key": "ak_local_dev_key",
}

#: HS256 的 HMAC 密钥下限，对齐 RFC 7518 §3.2（SHA-256 输出长度）。
_MIN_JWT_SECRET_BYTES = 32


class InsecureProductionConfigError(ValueError):
    """生产环境仍在用占位密钥/过短密钥。

    单独成类是为了让启动流程能把它与"配置字段写错"区分开：这个不是格式
    问题，是**部署没配密钥**，报错必须指名到具体的环境变量。
    """


def validate_production_secrets(settings: Settings) -> None:
    """env=production 时拒绝占位密钥与过短的 HMAC 密钥。

    `Settings.env` 此前是个只被写、从不被读的字段 —— 没有任何代码分支读它，
    于是 `ARIADNE_ENV=production` 与 development 的行为完全一致，生产部署会
    带着出厂的 `change-me-in-production` 签 JWT。这与"没配评分器却报 100 分"
    是同一类失效模式：系统看上去正常工作，安全属性却是空的。

    fail-fast 而不是打 warning：warning 在容器日志里滚过去没人看见，而这条
    的后果是任何人都能伪造管理员会话。
    """
    if settings.env.lower() not in {"production", "prod"}:
        return

    problems: list[str] = []
    for path, placeholder in _PLACEHOLDER_SECRETS.items():
        section, field = path.split(".")
        secret = getattr(getattr(settings, section), field).get_secret_value()
        if secret == placeholder:
            env_var = f"ARIADNE_{section.upper()}_{field.upper()}"
            problems.append(f"{path} 仍是出厂占位值，请设 {env_var}")

    jwt_len = len(settings.api.jwt_secret.get_secret_value().encode("utf-8"))
    if jwt_len < _MIN_JWT_SECRET_BYTES:
        problems.append(
            f"api.jwt_secret 只有 {jwt_len} 字节，HS256 至少需要 "
            f"{_MIN_JWT_SECRET_BYTES} 字节（RFC 7518 §3.2）"
        )

    if problems:
        raise InsecureProductionConfigError(
            "ARIADNE_ENV=production 但密钥配置不安全：" + "；".join(problems)
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
