import json
import subprocess

import pytest

from infra2_sdk._transport import HttpResponse
from infra2_sdk.runtime.config_schema import EnvironmentField, EnvironmentManifest
from infra2_sdk.secrets import (
    EnvBackend,
    OnePasswordBackend,
    ReadOnlyBackendError,
    SecretsError,
    SecretsResolver,
    VaultKvBackend,
    WriteResult,
    op_item,
    render_agent_policy,
    render_agent_template,
    vault_path,
)

MANIFEST = EnvironmentManifest(
    source="example.Settings",
    fields=(
        EnvironmentField(
            "twelve_data",
            "TWELVE_DATA_API_KEY",
            source="human",
            scope="project",
            sensitive=True,
            empty_ok=True,
        ),
        EnvironmentField(
            "sec_user_agent", "SEC_USER_AGENT", source="human", required=True, has_default=False
        ),
        EnvironmentField(
            "secret_key",
            "SECRET_KEY",
            source="runtime",
            sensitive=True,
            required=True,
            has_default=False,
        ),
        EnvironmentField(
            "admin_password",
            "ADMIN_PASSWORD",
            source="runtime",
            sensitive=True,
            required=True,
            has_default=False,
            mirror_to_1password=True,
        ),
        EnvironmentField(
            "database_url",
            "DATABASE_URL",
            source="runtime",
            group="postgres",
            provided_by="truealpha/postgres:POSTGRES_PASSWORD",
            composed_from="postgresql://postgres:{POSTGRES_PASSWORD}@db/app",
        ),
        EnvironmentField(
            "image_digest",
            "IMAGE_DIGEST",
            source="release",
            injected=True,
            required=True,
            has_default=False,
        ),
        EnvironmentField(
            "approved_by", "CAPTURE_APPROVED_BY", source="decision", injected=True, empty_ok=True
        ),
        EnvironmentField("budget", "MOOMOO_MONTHLY_CALL_BUDGET", source="code"),
        EnvironmentField(
            "s3_endpoint",
            "S3_ENDPOINT",
            source="code",
            composed_from="http://127.0.0.1:{env:TA_MINIO_S3_PORT}",
        ),
    ),
)


class MemoryBackend:
    def __init__(self, **paths: dict[str, str]) -> None:
        self.paths = {k: dict(v) for k, v in paths.items()}
        self.writes: list[tuple[str, dict[str, str]]] = []

    def read(self, path: str) -> dict[str, str]:
        return dict(self.paths.get(path, {}))

    def write(self, path: str, values: dict[str, str]) -> WriteResult:
        current = self.paths.setdefault(path, {})
        changed = tuple(sorted(k for k, v in values.items() if current.get(k) != v))
        current.update(values)
        self.writes.append((path, dict(values)))
        return WriteResult(changed)


def make_resolver(store: MemoryBackend, human: MemoryBackend | None = None, **kwargs):
    return SecretsResolver(
        MANIFEST,
        project="truealpha",
        service="data_engine",
        env="staging",
        store=store,
        human=human,
        generator=lambda field: f"generated-{field.env}",
        **kwargs,
    )


def test_paths_follow_the_infra2_coordinate() -> None:
    assert vault_path("truealpha", "staging", "data_engine") == "truealpha/staging/data_engine"
    assert op_item("truealpha", "staging", "data_engine") == "truealpha/staging/data_engine"
    assert op_item("truealpha", "staging", "data_engine", scope="project") == (
        "truealpha/shared/data_engine"
    )


def test_env_backend_reads_process_and_dotenv_and_refuses_writes(tmp_path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text('export A="from-file"\nB=file-b\n# comment\n', encoding="utf-8")
    backend = EnvBackend({"A": "from-env"}, dotenv_path=str(dotenv))
    assert backend.read("ignored") == {"A": "from-env", "B": "file-b"}
    with pytest.raises(ReadOnlyBackendError):
        backend.write("x", {"A": "1"})


def test_sync_human_reads_shared_and_env_items_and_writes_only_differences() -> None:
    human = MemoryBackend(
        **{
            "truealpha/shared/data_engine": {"TWELVE_DATA_API_KEY": "td-key"},
            "truealpha/staging/data_engine": {"SEC_USER_AGENT": "agent"},
        }
    )
    store = MemoryBackend(**{"truealpha/staging/data_engine": {"SEC_USER_AGENT": "agent"}})
    resolver = make_resolver(store, human)
    first = resolver.sync_human()
    assert first.changed == ("TWELVE_DATA_API_KEY",)
    assert first.unchanged == ("SEC_USER_AGENT",)
    assert first.ok
    again = make_resolver(store, human).sync_human()
    assert again.changed == () and not store.writes[1:]


def test_sync_human_reports_missing_required_values_without_writing_them() -> None:
    human = MemoryBackend(
        **{"truealpha/shared/data_engine": {}, "truealpha/staging/data_engine": {}}
    )
    store = MemoryBackend()
    report = make_resolver(store, human).sync_human()
    assert report.missing == ("SEC_USER_AGENT",)  # TWELVE_DATA is empty_ok
    assert not report.ok and store.writes == []


def test_ensure_runtime_generates_only_absent_values() -> None:
    store = MemoryBackend(**{"truealpha/staging/data_engine": {"SECRET_KEY": "keep"}})
    report = make_resolver(store).ensure_runtime()
    assert report.changed == ("ADMIN_PASSWORD",)
    assert report.unchanged == ("SECRET_KEY",)
    assert store.paths["truealpha/staging/data_engine"] == {
        "SECRET_KEY": "keep",
        "ADMIN_PASSWORD": "generated-ADMIN_PASSWORD",
    }
    assert make_resolver(store).ensure_runtime().changed == ()


def test_mirror_copies_flagged_runtime_values_to_the_human_store() -> None:
    store = MemoryBackend(**{"truealpha/staging/data_engine": {"ADMIN_PASSWORD": "pw"}})
    human = MemoryBackend()
    resolver = make_resolver(store, human)
    assert resolver.mirror().changed == ("ADMIN_PASSWORD",)
    assert human.paths["truealpha/staging/data_engine"] == {"ADMIN_PASSWORD": "pw"}
    assert resolver.mirror().unchanged == ("ADMIN_PASSWORD",)


def test_compose_env_takes_release_and_decision_values_never_the_store() -> None:
    store = MemoryBackend(**{"truealpha/staging/data_engine": {"IMAGE_DIGEST": "stale"}})
    resolver = make_resolver(store, release={"IMAGE_DIGEST": "sha256:" + "a" * 64})
    assert resolver.compose_env() == {"IMAGE_DIGEST": "sha256:" + "a" * 64}
    with pytest.raises(SecretsError, match="IMAGE_DIGEST"):
        make_resolver(store).compose_env()


def test_reconcile_uses_the_human_store_as_expected_values() -> None:
    human = MemoryBackend(
        **{
            "truealpha/shared/data_engine": {"TWELVE_DATA_API_KEY": "new"},
            "truealpha/staging/data_engine": {"SEC_USER_AGENT": "agent"},
        }
    )
    store = MemoryBackend(
        **{
            "truealpha/staging/data_engine": {
                "TWELVE_DATA_API_KEY": "old",
                "SEC_USER_AGENT": "agent",
                "SECRET_KEY": "",
                "LEGACY": "x",
                "_drift_probe": "",
            }
        }
    )
    report = make_resolver(store, human).reconcile()
    assert report.to_dict() == {
        "missing": ["ADMIN_PASSWORD"],
        "empty": ["SECRET_KEY"],
        "unclassified": ["LEGACY"],
        "stale": ["TWELVE_DATA_API_KEY"],
    }


# ----------------------------------------------------------------------------- Vault


class FakeVault:
    def __init__(self, data: dict[str, str] | None = None) -> None:
        self.data = data
        self.version = 1 if data else 0
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def __call__(self, method, url, headers, body) -> HttpResponse:
        self.calls.append((method, url, dict(headers), body))
        if url.endswith("/auth/approle/login"):
            return HttpResponse(200, {}, json.dumps({"auth": {"client_token": "tok"}}).encode())
        if headers.get("X-Vault-Token") != "tok":
            return HttpResponse(403, {}, b"")
        if method == "GET":
            if self.data is None:
                return HttpResponse(404, {}, b"")
            payload = {"data": {"data": self.data, "metadata": {"version": self.version}}}
            return HttpResponse(200, {}, json.dumps(payload).encode())
        parsed = json.loads(body or b"{}")["data"]
        if method == "PATCH":
            assert headers["Content-Type"] == "application/merge-patch+json"
            self.data.update(parsed)
        else:
            self.data = dict(parsed)
        self.version += 1
        return HttpResponse(200, {}, b"{}")


def test_vault_backend_reads_patches_only_changed_keys_and_creates_when_absent() -> None:
    fake = FakeVault({"A": "1", "B": "2"})
    backend = VaultKvBackend("https://vault.example", token="tok", transport=fake)
    assert backend.read("p/staging/s") == {"A": "1", "B": "2"}
    assert backend.write("p/staging/s", {"A": "1", "B": "3"}) == WriteResult(("B",))
    assert fake.calls[-1][0] == "PATCH"
    assert json.loads(fake.calls[-1][3]) == {"data": {"B": "3"}}
    assert backend.write("p/staging/s", {"A": "1", "B": "3"}) == WriteResult()
    assert backend.read_version("p/staging/s") == 2
    assert all(c[2]["User-Agent"].startswith("infra2-sdk") for c in fake.calls)

    empty = FakeVault(None)
    backend = VaultKvBackend("https://vault.example", token="tok", transport=empty)
    assert backend.read("p/staging/new") == {}
    assert backend.write("p/staging/new", {"A": "1"}) == WriteResult(("A",))
    assert empty.calls[-1][0] == "POST"


def test_vault_backend_from_environ_prefers_token_then_approle() -> None:
    fake = FakeVault({})
    backend = VaultKvBackend.from_environ(
        {"VAULT_ADDR": "https://vault.example", "VAULT_ROLE_ID": "r", "VAULT_SECRET_ID": "s"},
        transport=fake,
    )
    assert fake.calls[0][1].endswith("/v1/auth/approle/login")
    assert backend.read("x") == {}
    with pytest.raises(SecretsError, match="VAULT_TOKEN or VAULT_ROLE_ID"):
        VaultKvBackend.from_environ({"VAULT_ADDR": "https://vault.example"}, transport=fake)


def test_vault_errors_never_include_values() -> None:
    fake = FakeVault({"A": "1"})
    backend = VaultKvBackend("https://vault.example", token="wrong", transport=fake)
    with pytest.raises(SecretsError, match="HTTP 403") as info:
        backend.read("p/staging/s")
    assert "1" not in str(info.value).split("HTTP")[0]


# ----------------------------------------------------------------------------- 1Password


class FakeOp:
    def __init__(self, items: dict[str, dict] | None = None) -> None:
        self.items = items or {}
        self.commands: list[list[str]] = []

    def __call__(self, args, capture_output, text):
        self.commands.append(list(args))
        verb, noun = args[1], args[2]
        if (verb, noun) == ("item", "get"):
            item = self.items.get(args[3])
            if item is None:
                return subprocess.CompletedProcess(
                    args, 1, "", '"x" isn\'t an item in the "Infra2" vault'
                )
            return subprocess.CompletedProcess(args, 0, json.dumps(item), "")
        if (verb, noun) == ("item", "create"):
            title = next(a for a in args if a.startswith("--title=")).split("=", 1)[1]
            fields = [
                {"label": a.split("[", 1)[0], "value": a.split("=", 1)[1]}
                for a in args
                if "[password]=" in a
            ]
            self.items[title] = {"id": "id-" + title, "fields": fields}
            return subprocess.CompletedProcess(args, 0, "", "")
        if (verb, noun) == ("item", "edit"):
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 1, "", "unexpected")


def test_onepassword_backend_reads_labels_creates_and_edits_by_id_with_sections() -> None:
    fake = FakeOp(
        {
            "truealpha/staging/app": {
                "id": "abc",
                "fields": [
                    {"label": "username", "purpose": "USERNAME", "value": ""},
                    {"label": "notesPlain", "purpose": "NOTES", "value": "n"},
                    {"label": "SECRET_KEY", "value": "old", "section": {"label": "app"}},
                ],
            }
        }
    )
    backend = OnePasswordBackend("Infra2", runner=fake)
    assert backend.read("truealpha/staging/app") == {"username": "", "SECRET_KEY": "old"}
    assert backend.read("missing/item") == {}

    result = backend.write("truealpha/staging/app", {"SECRET_KEY": "new", "OTHER": "v"})
    assert result.changed == ("OTHER", "SECRET_KEY")
    edit = fake.commands[-1]
    assert edit[:4] == ["op", "item", "edit", "abc"]
    assert "app.SECRET_KEY[password]=new" in edit and "OTHER[password]=v" in edit
    assert backend.write("truealpha/staging/app", {"SECRET_KEY": "old"}) == WriteResult()

    created = backend.write("truealpha/shared/app", {"ZAI_API_KEY": "k"})
    assert created.changed == ("ZAI_API_KEY",)
    assert fake.commands[-1][:3] == ["op", "item", "create"]
    assert "--title=truealpha/shared/app" in fake.commands[-1]


def test_onepassword_errors_carry_only_the_last_stderr_line() -> None:
    def failing(args, capture_output, text):
        return subprocess.CompletedProcess(
            args, 1, "", "line1\n[ERROR] 2026 (403) Service Account Deleted"
        )

    backend = OnePasswordBackend(runner=failing)
    with pytest.raises(SecretsError, match="Service Account Deleted"):
        backend.read("x")


# ----------------------------------------------------------------------------- renderers


def test_agent_template_renders_only_store_backed_and_provided_fields() -> None:
    text = render_agent_template(MANIFEST, project="truealpha", service="data_engine")
    assert text == (
        "{{- /* Generated from example.Settings by infra2_sdk.secrets. Do not edit. */ -}}\n"
        '{{- $env := env "ENV" -}}\n'
        '{{- with secret (printf "secret/data/truealpha/%s/postgres" $env) }}\n'
        'DATABASE_URL={{ printf "%q" (printf "postgresql://postgres:%s@db/app" '
        ".Data.data.POSTGRES_PASSWORD) }}\n"
        "{{- end }}\n"
        '{{- with secret (printf "secret/data/truealpha/%s/data_engine" $env) }}\n'
        'TWELVE_DATA_API_KEY={{ with .Data.data.TWELVE_DATA_API_KEY }}{{ printf "%q" . }}'
        '{{ else }}""{{ end }}\n'
        'SEC_USER_AGENT={{ printf "%q" .Data.data.SEC_USER_AGENT }}\n'
        'SECRET_KEY={{ printf "%q" .Data.data.SECRET_KEY }}\n'
        'ADMIN_PASSWORD={{ printf "%q" .Data.data.ADMIN_PASSWORD }}\n'
        'S3_ENDPOINT={{ printf "%q" (printf "http://127.0.0.1:%s" (env "TA_MINIO_S3_PORT")) }}\n'
        "{{- end }}\n"
    )
    assert "IMAGE_DIGEST" not in text and "MOOMOO" not in text and "CAPTURE_APPROVED_BY" not in text


def test_agent_template_can_pin_a_source_env_and_exclude_groups() -> None:
    text = render_agent_template(
        MANIFEST,
        project="truealpha",
        service="app",
        source_env="staging",
        exclude_groups=("postgres",),
    )
    assert '(printf "secret/data/truealpha/%s/app" "staging")' in text
    assert "postgres" not in text and "DATABASE_URL" not in text


def test_agent_policy_covers_own_and_provider_paths_once() -> None:
    policy = render_agent_policy(MANIFEST, project="truealpha", service="data_engine")
    assert policy.count('path "secret/data/truealpha/{{env}}/data_engine"') == 1
    assert policy.count('path "secret/data/truealpha/{{env}}/postgres"') == 1
    assert 'path "secret/metadata/truealpha/{{env}}/postgres"' in policy
    assert policy.rstrip().endswith('path "auth/token/lookup-self" {\n  capabilities = ["read"]\n}')
    pinned = render_agent_policy(MANIFEST, project="truealpha", service="app", source_env="staging")
    assert "{{env}}" not in pinned and "secret/data/truealpha/staging/app" in pinned
