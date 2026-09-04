"""Helm chart 与 Python 侧的契约测试。

这批断言存在的理由：chart 里写错的名字不会有任何人报错。
- pydantic-settings 对无匹配字段的环境变量静默忽略，所以 ARIADNE_PG_URL
  这种"看着对"的变量落地为零效果，全部字段回落默认值（localhost）。
- `python -m ariadne.cli migrate` 因为 cli.py 没有 __main__ 块，只导入模块
  就以 0 退出。Helm 把它当成"迁移成功"，继续把应用部署到空 schema 上。

两类都表现为部署期的诡异故障而非配置错误，所以在这里做静态校验：
不需要 helm 二进制，也不需要集群。

TestRenderedEnvDrivesSettings 更进一步：用 tests/helm_render.py 把 helper
真的渲染出来，再把结果灌进设置类，断言 DSN 指向集群而非 localhost。
名字对不上就会在这里表现为「回落默认值」，而不是靠人眼比对。

不覆盖的部分：完整清单渲染（Deployment/Job 骨架用了 toYaml、nindent、
dict 等本渲染器不支持的构造），以及 chart 与真实集群的交互。
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

import pytest
import yaml
from pydantic_settings import BaseSettings

from ariadne.config import (
    ClickHouseSettings,
    PayloadSettings,
    PostgresSettings,
    RedisSettings,
)
from helm_render import render_define

REPO_ROOT = Path(__file__).resolve().parents[1]
CHART_DIR = REPO_ROOT / "deploy" / "helm"
TEMPLATES_DIR = CHART_DIR / "templates"

# botocore 直接读的变量，不经 ARIADNE_ 前缀，所以不在设置类里
_AWS_PASSTHROUGH = {
    "AWS_ENDPOINT_URL_S3",
    "AWS_DEFAULT_REGION",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
}

_SETTINGS_CLASSES: tuple[type[BaseSettings], ...] = (
    ClickHouseSettings,
    PostgresSettings,
    RedisSettings,
    PayloadSettings,
)


def _accepted_env_names() -> set[str]:
    """设置类真正会接受的环境变量全名（前缀 + 字段名，大写）。"""
    names: set[str] = set()
    for cls in _SETTINGS_CLASSES:
        prefix = cls.model_config.get("env_prefix", "")
        for field in cls.model_fields:
            names.add(f"{prefix}{field}".upper())
    return names


def _chart_env_names() -> dict[str, list[str]]:
    """扫 templates/ 与 values.yaml 里出现的环境变量名 -> 出现的文件。"""
    found: dict[str, list[str]] = {}
    sources = list(TEMPLATES_DIR.glob("*.tpl")) + list(TEMPLATES_DIR.glob("*.yaml"))
    sources.append(CHART_DIR / "values.yaml")
    for path in sources:
        text = path.read_text(encoding="utf-8")
        # 两种写法，分开匹配：
        #   模板：  - name: ARIADNE_X        （名字后无冒号）
        #   values：  ARIADNE_X: "值"        （env 映射，名字后有冒号）
        # values.yaml 那半边别漏 —— 同样会写出静默失效的名字。
        pattern = r"^\s*-\s*name:\s*([A-Z][A-Z0-9_]{3,})\s*$|^\s+([A-Z][A-Z0-9_]{3,})\s*:"
        for match in re.finditer(pattern, text, re.M):
            name = match.group(1) or match.group(2)
            found.setdefault(name, []).append(path.name)
    return found


class TestEnvNamesMatchSettings:
    """chart 发出的每个 ARIADNE_* 变量都必须有对应字段。"""

    def test_chart_emits_env_vars_at_all(self) -> None:
        """先证明扫描确实抓到了东西，否则下面的断言是空过。"""
        names = _chart_env_names()
        assert "ARIADNE_PG_HOST" in names
        assert "ARIADNE_CH_HOST" in names

    def test_every_ariadne_var_has_a_settings_field(self) -> None:
        accepted = _accepted_env_names()
        # 其他前缀的设置类（LLM / API / HARNESS…）不在本测试范围，
        # 只校验存储相关的四个前缀
        checked_prefixes = ("ARIADNE_CH_", "ARIADNE_PG_", "ARIADNE_REDIS_", "ARIADNE_PAYLOAD_")
        orphans = {
            name: files
            for name, files in _chart_env_names().items()
            if name.startswith(checked_prefixes) and name not in accepted
        }
        assert not orphans, (
            f"这些变量在 chart 里出现但没有任何设置字段接收，会被静默忽略：{orphans}"
        )

    def test_no_url_style_vars_for_pg_and_ch(self) -> None:
        """回归锚点：ARIADNE_PG_URL / ARIADNE_CH_URL 是曾经的具体缺陷。

        这两个类没有 url 字段（DSN 由 _dsn_for 拼装并做百分号转义），
        写 url 形式等于什么都没配。
        """
        names = _chart_env_names()
        assert "ARIADNE_PG_URL" not in names
        assert "ARIADNE_CH_URL" not in names

    def test_clickhouse_user_field_is_user_not_username(self) -> None:
        """ClickHouseSettings 的字段是 user；曾经发的是 ARIADNE_CH_USERNAME。"""
        names = _chart_env_names()
        assert "ARIADNE_CH_USER" in names
        assert "ARIADNE_CH_USERNAME" not in names

    def test_s3_endpoint_uses_aws_standard_names(self) -> None:
        """S3ObjectStore 是裸 boto3.client("s3")，endpoint/region 只能走 AWS 变量。"""
        names = _chart_env_names()
        assert "AWS_ENDPOINT_URL_S3" in names
        assert "AWS_DEFAULT_REGION" in names
        assert "ARIADNE_S3_ENDPOINT" not in names
        assert "ARIADNE_S3_REGION" not in names

    def test_aws_passthrough_names_are_intentional(self) -> None:
        """AWS_* 不该有拼写变体混进来。"""
        aws_in_chart = {n for n in _chart_env_names() if n.startswith("AWS_")}
        unexpected = aws_in_chart - _AWS_PASSTHROUGH
        assert not unexpected, f"未预期的 AWS 变量：{unexpected}"


class TestOwnerCredentialSplit:
    """owner 凭据只给迁移 Job —— 应用 Pod 拿到就等于 RLS 可绕过。"""

    def test_storage_env_has_no_owner_password(self) -> None:
        helpers = (TEMPLATES_DIR / "_helpers.tpl").read_text(encoding="utf-8")
        storage_block = helpers.split('define "ariadne.storageEnv"')[1].split(
            'define "ariadne.payloadEnv"'
        )[0]
        assert "ARIADNE_PG_APP_USER" in storage_block
        assert "ARIADNE_PG_APP_PASSWORD" in storage_block
        # owner 的两个变量必须缺席（注意 APP_ 前缀不算）
        assert not re.search(r"name:\s*ARIADNE_PG_PASSWORD\b", storage_block)
        assert not re.search(r"name:\s*ARIADNE_PG_USER\b", storage_block)

    def test_owner_env_included_only_by_migrate(self) -> None:
        includers = [
            path.name
            for path in TEMPLATES_DIR.glob("*.yaml")
            if "ariadne.pgOwnerEnv" in path.read_text(encoding="utf-8")
        ]
        assert includers == ["migrate.yaml"]

    def test_migrate_runs_before_app_pods(self) -> None:
        """app Pod 只带 app 凭据的前提：角色在它们启动前已建好。"""
        migrate = (TEMPLATES_DIR / "migrate.yaml").read_text(encoding="utf-8")
        assert "pre-install,pre-upgrade" in migrate
        assert '"helm.sh/hook-weight": "-5"' in migrate

    def test_app_user_is_configured(self) -> None:
        """appUser 留空会让 app_dsn() 回落 owner，RLS 形同虚设。"""
        values = yaml.safe_load((CHART_DIR / "values.yaml").read_text(encoding="utf-8"))
        assert values["global"]["postgres"]["appUser"]


class TestCommandsUseConsoleScripts:
    """`python -m ariadne.cli ...` 退出 0 而什么都不做 —— 曾经五处全中。"""

    @pytest.fixture(scope="class")
    def console_scripts(self) -> set[str]:
        pyproject = tomllib.loads(
            (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        return set(pyproject["project"]["scripts"])

    def _chart_commands(self) -> list[tuple[str, list[str]]]:
        """(来源文件, argv) —— 只取内联 JSON 数组形式的 command。"""
        commands: list[tuple[str, list[str]]] = []
        sources = [*TEMPLATES_DIR.glob("*.yaml"), CHART_DIR / "values.yaml"]
        for path in sources:
            for match in re.finditer(
                r"^\s*command:\s*(\[[^\]]*\])\s*$", path.read_text(encoding="utf-8"), re.M
            ):
                commands.append((path.name, yaml.safe_load(match.group(1))))
        return commands

    def test_all_five_commands_are_found(self) -> None:
        """api + collector + loop + eval + migrate = 5。少了说明扫描漏了。"""
        assert len(self._chart_commands()) == 5

    def test_no_command_uses_python_dash_m(self) -> None:
        offenders = [
            (src, argv)
            for src, argv in self._chart_commands()
            if argv[:2] == ["python", "-m"]
        ]
        assert not offenders, (
            f"cli.py 没有 __main__ 块，这些命令会立刻以 0 退出：{offenders}"
        )

    def test_every_command_entrypoint_is_declared(self, console_scripts: set[str]) -> None:
        for src, argv in self._chart_commands():
            assert argv[0] in console_scripts, f"{src} 的 {argv[0]} 不在 [project.scripts] 里"

    def test_worker_subcommands_are_recognized(self) -> None:
        """run_worker 只认 loop / eval，其余回落 collector —— 拼错会静默跑错角色。"""
        subcommands = {
            argv[1]
            for _, argv in self._chart_commands()
            if argv[0] == "ariadne-worker" and len(argv) > 1
        }
        assert subcommands == {"collector", "loop", "eval"}


class TestPayloadBackend:
    """配了 bucket 但 store_backend 还是 local，payload 会写进临时容器盘。"""

    def test_store_backend_is_set_when_bucket_configured(self) -> None:
        values = yaml.safe_load((CHART_DIR / "values.yaml").read_text(encoding="utf-8"))
        assert values["global"]["s3"]["bucket"], "前提变了，这条测试要跟着改"

        helpers = (TEMPLATES_DIR / "_helpers.tpl").read_text(encoding="utf-8")
        payload_block = helpers.split('define "ariadne.payloadEnv"')[1]
        assert "ARIADNE_PAYLOAD_STORE_BACKEND" in payload_block
        assert 'value: "s3"' in payload_block

    def test_default_backend_is_local(self, clean_env: None) -> None:
        """chart 不设时的 Python 侧默认值 —— 说明为什么 chart 必须显式设。

        _env_file=None 是必需的：设置类会读仓库根的 .env（未纳入版本管理），
        本机往里加一行 ARIADNE_PAYLOAD_STORE_BACKEND 就能让这条断言变色。
        断言的对象是代码默认值，所以要把文件来源一起摘掉，不只是环境变量。
        """
        assert PayloadSettings(_env_file=None).store_backend == "local"


class TestMigrationAssetsShipInImage:
    """alembic.ini 与 deploy/ 不在 wheel 里（hatch 只打包 src/）。"""

    def test_dockerfile_copies_alembic_ini(self) -> None:
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        assert re.search(r"^COPY\b.*\balembic\.ini\b", dockerfile, re.M)

    def test_wheel_packages_exclude_deploy(self) -> None:
        """前提校验：deploy/ 真的不在 wheel 里，所以 cli 必须按 cwd 找。"""
        pyproject = tomllib.loads(
            (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        packages = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
        assert all(not p.startswith("deploy") for p in packages)


# ---- 渲染级验证 ----

_HELPERS = TEMPLATES_DIR / "_helpers.tpl"
_STORAGE_ENV = "ariadne.storageEnv"
_OWNER_ENV = "ariadne.pgOwnerEnv"


def _load_values() -> dict:
    return yaml.safe_load((CHART_DIR / "values.yaml").read_text(encoding="utf-8"))


def _sentinel_values() -> dict:
    """把 values 换成不可能与 Python 默认值相撞的哨兵。

    必要性：chart 的 port/database/user/prefix 恰好和设置类默认值相同，
    直接断言"字段 == chart 值"时，变量名写错也会因回落默认值而通过。
    哨兵让"名字有没有接上"成为唯一可能的解释。
    """
    values = _load_values()
    values["global"]["clickhouse"].update(
        host="ch-sentinel", port=9001, database="chdb-sentinel", user="chuser-sentinel"
    )
    values["global"]["postgres"].update(
        host="pg-sentinel",
        port=5433,
        database="pgdb-sentinel",
        user="owner-sentinel",
        password="owner-pw-sentinel",
        appUser="appuser-sentinel",
        appPassword="app-pw-sentinel",
    )
    values["global"]["redis"]["url"] = "redis://redis-sentinel:6399/7"
    values["global"]["s3"].update(
        bucket="bucket-sentinel",
        prefix="prefix-sentinel",
        region="region-sentinel",
        endpoint="http://endpoint-sentinel:1",
    )
    return values


def _render(define: str, values: dict) -> list[dict]:
    """渲染 helper 并解析成 YAML 列表 —— 解析失败即空白裁剪写错了。"""
    text = render_define(_HELPERS.read_text(encoding="utf-8"), define, values)
    parsed = yaml.safe_load(text)
    assert isinstance(parsed, list), f"{define} 渲染结果不是 YAML 列表：\n{text}"
    return parsed


def _plain(entries: list[dict]) -> dict[str, str]:
    """明文 value 的部分 —— 可以直接当环境变量灌进设置类。"""
    return {e["name"]: e["value"] for e in entries if "value" in e}


def _secret_refs(entries: list[dict]) -> dict[str, tuple[str, str]]:
    """valueFrom.secretKeyRef 的部分 -> (secret 名, key)。"""
    return {
        e["name"]: (e["valueFrom"]["secretKeyRef"]["name"], e["valueFrom"]["secretKeyRef"]["key"])
        for e in entries
        if "valueFrom" in e
    }


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清掉宿主机上的 ARIADNE_/AWS_ 变量。

    否则本机已有的值会替渲染结果"兜底"，名字写错也能通过 —— 正是要防的那种空过。
    """
    for key in list(os.environ):
        if key.startswith(("ARIADNE_", "AWS_")):
            monkeypatch.delenv(key, raising=False)


class TestRenderedEnvDrivesSettings:
    """把渲染结果灌进设置类：名字对不上就会回落 localhost，在这里现形。"""

    def test_storage_env_parses_as_yaml_list(self) -> None:
        """{{- /* */}} 少写一个减号就会多出悬空缩进，YAML 解析先挡住。"""
        entries = _render(_STORAGE_ENV, _load_values())
        assert len(entries) >= 10
        assert all(set(e) <= {"name", "value", "valueFrom"} for e in entries)

    def test_app_dsn_points_at_cluster_not_localhost(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """验收核心：app 容器只拿 storageEnv，DSN 必须是 ariadne_app@postgres。"""
        values = _load_values()
        for name, value in _plain(_render(_STORAGE_ENV, values)).items():
            monkeypatch.setenv(name, value)

        pg = PostgresSettings()
        app_dsn = pg.app_dsn()
        assert values["global"]["postgres"]["appUser"] in app_dsn
        assert values["global"]["postgres"]["host"] in app_dsn
        assert "localhost" not in app_dsn
        # app 容器没有 owner 凭据，所以 dsn() 只能是 PostgresSettings 的默认 user
        assert app_dsn != pg.dsn()

    def test_no_rendered_value_falls_back_to_localhost(self) -> None:
        """回归锚点：曾经 7 个变量名无人接收，全部字段回落 localhost。"""
        rendered = _plain(_render(_STORAGE_ENV, _load_values()))
        assert "localhost" not in " ".join(rendered.values())

    def test_every_rendered_name_lands_on_a_settings_field(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """哨兵值全程送达 —— 任一变量名写错，对应字段就会停在默认值上。"""
        values = _sentinel_values()
        for name, value in _plain(_render(_STORAGE_ENV, values)).items():
            monkeypatch.setenv(name, value)

        ch = ClickHouseSettings()
        assert (ch.host, ch.port, ch.database, ch.user) == (
            "ch-sentinel",
            9001,
            "chdb-sentinel",
            "chuser-sentinel",
        )

        pg = PostgresSettings()
        assert (pg.host, pg.port, pg.database) == ("pg-sentinel", 5433, "pgdb-sentinel")
        assert pg.app_user == "appuser-sentinel"
        assert pg.app_password.get_secret_value() == "app-pw-sentinel"

        payload = PayloadSettings()
        assert payload.store_backend == "s3"
        assert payload.s3_bucket == "bucket-sentinel"
        assert payload.s3_prefix == "prefix-sentinel"

        assert RedisSettings().url == "redis://redis-sentinel:6399/7"

    def test_existing_secret_switches_to_secret_key_ref(self) -> None:
        """配了 existingSecret 就不该再有明文密码落到 values 渲染结果里。"""
        values = _load_values()
        values["global"]["clickhouse"]["existingSecret"] = "ch-secret"
        values["global"]["postgres"]["existingSecret"] = "pg-secret"
        values["global"]["redis"]["existingSecret"] = "redis-secret"
        values["global"]["s3"]["existingSecret"] = "s3-secret"

        entries = _render(_STORAGE_ENV, values)
        refs = _secret_refs(entries)
        assert refs["ARIADNE_CH_PASSWORD"] == ("ch-secret", "password")
        assert refs["ARIADNE_CH_USER"] == ("ch-secret", "username")
        assert refs["ARIADNE_PG_APP_PASSWORD"] == ("pg-secret", "appPassword")
        assert refs["ARIADNE_REDIS_PASSWORD"] == ("redis-secret", "password")
        assert refs["AWS_ACCESS_KEY_ID"] == ("s3-secret", "accessKeyId")
        # 明文分支必须让位，否则两个同名 env 后者覆盖前者，行为取决于顺序
        assert "ARIADNE_PG_APP_PASSWORD" not in _plain(entries)
        assert "ARIADNE_CH_USER" not in _plain(entries)

    def test_migrate_container_gets_owner_dsn(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """迁移容器 = storageEnv + pgOwnerEnv，两份都在时 owner DSN 才成立。

        用哨兵而非 values 原值：owner 默认值就是 ariadne，与 chart 的 owner 相同，
        拿原值断言时把 ARIADNE_PG_USER 写错也照样通过（变异检查抓到过这一点）。
        """
        values = _sentinel_values()
        merged = {
            **_plain(_render(_STORAGE_ENV, values)),
            **_plain(_render(_OWNER_ENV, values)),
        }
        for name, value in merged.items():
            monkeypatch.setenv(name, value)

        pg = PostgresSettings()
        assert pg.user == "owner-sentinel"
        assert pg.password.get_secret_value() == "owner-pw-sentinel"
        assert pg.dsn().startswith("postgresql+asyncpg://owner-sentinel:")
        assert "pg-sentinel" in pg.dsn()
        assert pg.dsn() != pg.app_dsn()

    def test_empty_app_user_silently_degrades_to_owner(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """记录这个陷阱：appUser 留空不会报错，而是静默拿到 owner 权限。

        PostgresSettings 的 owner 默认值是 ariadne/ariadne，与 chart 默认 owner
        一致，所以回落能连上 —— RLS 直接失效且日志里什么都看不到。
        这也是 test_app_user_is_configured 存在的理由。
        """
        values = _load_values()
        values["global"]["postgres"]["appUser"] = ""
        values["global"]["postgres"]["appPassword"] = ""
        for name, value in _plain(_render(_STORAGE_ENV, values)).items():
            monkeypatch.setenv(name, value)

        pg = PostgresSettings()
        assert pg.app_dsn() == pg.dsn()

    def test_port_is_rendered_as_string(self) -> None:
        """k8s 的 env.value 必须是字符串，整数会被 API server 拒掉。"""
        rendered = _plain(_render(_STORAGE_ENV, _load_values()))
        assert rendered["ARIADNE_PG_PORT"] == "5432"
        assert rendered["ARIADNE_CH_PORT"] == "8123"

    def test_empty_bucket_omits_payload_vars(self) -> None:
        """bucket 留空走容器本地盘，此时不该出现半套 S3 配置。"""
        values = _load_values()
        values["global"]["s3"]["bucket"] = ""
        rendered = _plain(_render(_STORAGE_ENV, values))
        assert not [n for n in rendered if n.startswith(("ARIADNE_PAYLOAD_", "AWS_"))]
