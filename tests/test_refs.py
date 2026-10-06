import json
import subprocess
from types import SimpleNamespace

import pytest

from infra2_sdk import refs
from infra2_sdk.refs import (
    ReleaseError,
    ReleaseIdentity,
    classify_ref,
    ls_remote_rows,
    redact_repo,
    resolve_image_digest,
    resolve_image_ref,
    resolve_pr,
    resolve_release_identity,
    resolve_to_sha,
    verify_runtime_identity,
)
from infra2_sdk.runtime.identity import RuntimeIdentity
from infra2_sdk.transport import HttpResponse

SHA = "1234567890abcdef1234567890abcdef12345678"
TAG_OBJECT = "a" * 40
REPO = "https://github.com/wangzitian0/example.git"


def runner(stdout: str = "", *, error: Exception | None = None):
    def run(command, **kwargs):
        assert command[:2] == ["git", "ls-remote"]
        assert kwargs["timeout"] == 30
        if error:
            raise error
        return SimpleNamespace(stdout=stdout)

    return run


def test_classify_ref() -> None:
    assert classify_ref("main") == "branch"
    assert classify_ref("v1.2.3") == "tag"
    assert classify_ref(SHA) == "sha"
    with pytest.raises(ValueError, match="unrecognized"):
        classify_ref("feature/example")


def test_annotated_tag_prefers_peeled_commit() -> None:
    output = f"{TAG_OBJECT}\trefs/tags/v1.2.3\n{SHA}\trefs/tags/v1.2.3^{{}}\n"
    resolved = resolve_image_ref("v1.2.3", repo=REPO, runner=runner(output))
    assert resolved.sha == SHA
    assert resolved.image_ref == "v1.2.3"


def test_branch_and_pr_resolution() -> None:
    branch = resolve_to_sha(
        "main",
        repo=REPO,
        runner=runner(f"{SHA}\trefs/heads/main\n"),
    )
    pr = resolve_pr(
        42,
        repo=REPO,
        runner=runner(f"{SHA}\trefs/pull/42/head\n"),
    )
    assert branch == SHA
    assert pr.image_ref == SHA[:7]
    assert pr.form == "pr"


def test_authenticated_repo_is_redacted_on_git_failure() -> None:
    authenticated = "https://secret-token@github.com/wangzitian0/private.git"
    error = subprocess.CalledProcessError(1, ["git", "ls-remote", authenticated])
    with pytest.raises(ValueError) as exc_info:
        resolve_to_sha("main", repo=authenticated, runner=runner(error=error))
    assert "secret-token" not in str(exc_info.value)
    assert "<redacted>" in str(exc_info.value)


def test_sha_resolution_is_local_and_normalized() -> None:
    def must_not_run(*args, **kwargs):
        raise AssertionError("runner should not be called for a sha")

    assert resolve_to_sha(SHA.upper(), repo=REPO, runner=must_not_run) == SHA


def test_lightweight_tag_uses_plain_ref() -> None:
    resolved = resolve_image_ref(
        "v1.2.3",
        repo=REPO,
        runner=runner(f"{SHA}\trefs/tags/v1.2.3\n"),
    )
    assert resolved.sha == SHA


def test_missing_remote_refs_fail_closed() -> None:
    with pytest.raises(ValueError, match="not found"):
        resolve_to_sha("main", repo=REPO, runner=runner())
    with pytest.raises(ValueError, match="tag .* not found"):
        resolve_image_ref("v1.2.3", repo=REPO, runner=runner())
    with pytest.raises(ValueError, match="PR #42 head not found"):
        resolve_pr(42, repo=REPO, runner=runner())


@pytest.mark.parametrize("value", [0, "", "abc", -1])
def test_pr_resolution_requires_positive_integer(value) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        resolve_pr(value, repo=REPO, runner=runner())


def test_malformed_remote_rows_are_ignored() -> None:
    with pytest.raises(ValueError, match="not found"):
        resolve_to_sha("main", repo=REPO, runner=runner("malformed\n\trefs/heads/main\n"))


def test_os_error_is_wrapped() -> None:
    with pytest.raises(ValueError, match="git ls-remote failed"):
        resolve_to_sha("main", repo=REPO, runner=runner(error=OSError("git missing")))


def test_command_runner_protocol() -> None:
    from infra2_sdk.refs import CommandRunner

    # Verify a custom callable satisfies CommandRunner
    class CustomRunner:
        def __call__(self, args, **kwargs):
            return SimpleNamespace(stdout="custom\trefs/heads/main\n")

    c = CustomRunner()
    assert isinstance(c, CommandRunner)
    assert resolve_to_sha("main", repo=REPO, runner=c) == "custom"


def test_ls_remote_rows_is_public_and_passes_the_command_through() -> None:
    seen: list[list[str]] = []

    def run(command, **kwargs):
        seen.append(command)
        assert (kwargs["capture_output"], kwargs["text"], kwargs["check"]) == (True, True, True)
        assert kwargs["timeout"] == 30
        return SimpleNamespace(stdout=f"{SHA}\trefs/heads/main\n\nmalformed\n\trefs/x\n")

    rows = ls_remote_rows(REPO, "refs/heads/main", "refs/tags/v1.0.0", runner=run)
    assert rows == [(SHA, "refs/heads/main")]
    assert seen == [["git", "ls-remote", REPO, "refs/heads/main", "refs/tags/v1.0.0"]]
    assert ls_remote_rows(REPO, runner=lambda *a, **k: SimpleNamespace(stdout=None)) == []


def test_ls_remote_rows_defaults_to_subprocess_run_like_the_other_resolvers() -> None:
    assert ls_remote_rows.__kwdefaults__["runner"] is subprocess.run


@pytest.mark.parametrize(
    "error",
    [
        OSError("git missing"),
        subprocess.TimeoutExpired(["git", "ls-remote"], 30),
        subprocess.CalledProcessError(
            128, ["git", "ls-remote", "https://user:secret-token@github.com/o/r.git"]
        ),
    ],
)
def test_ls_remote_rows_wraps_failures_and_never_leaks_embedded_credentials(error) -> None:
    authenticated = "https://user:secret-token@github.com/o/r.git"
    with pytest.raises(ValueError, match="git ls-remote failed") as exc_info:
        ls_remote_rows(authenticated, "refs/heads/main", runner=runner(error=error))
    assert "secret-token" not in str(exc_info.value)
    assert "<redacted>@github.com" in str(exc_info.value)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("https://ghp_secret@github.com/x/y.git", "https://<redacted>@github.com/x/y.git"),
        ("https://user:pass@github.com/x/y.git", "https://<redacted>@github.com/x/y.git"),
        ("https://github.com/x/y.git", "https://github.com/x/y.git"),
        ("git@github.com:x/y.git", "git@github.com:x/y.git"),
        (
            "fatal: https://a:b@h/x and http://c@h/y",
            "fatal: https://<redacted>@h/x and http://<redacted>@h/y",
        ),
    ],
)
def test_redact_repo_is_public_and_strips_url_credentials(value: str, expected: str) -> None:
    assert redact_repo(value) == expected


def test_private_names_remain_as_deprecated_aliases_of_the_public_functions() -> None:
    # infra2's libs/deploy/refs.py imports these two private spellings until it migrates.
    assert refs._ls_remote_rows is refs.ls_remote_rows
    assert refs._redact_repo is refs.redact_repo


# --- Release Identity & Verification (consolidated from release) --------------

RELEASE_SHA = "0123456789abcdef0123456789abcdef01234567"
RELEASE_DIGEST = "sha256:" + "f" * 64


class FakeRegistry:
    def __init__(self, *, manifest_status: int = 200, token_status: int = 200) -> None:
        self.manifest_status = manifest_status
        self.token_status = token_status
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def __call__(self, method, url, headers, body) -> HttpResponse:
        self.calls.append((method, url, dict(headers)))
        if "/token?" in url:
            return HttpResponse(self.token_status, {}, json.dumps({"token": "anon"}).encode())
        return HttpResponse(self.manifest_status, {"docker-content-digest": RELEASE_DIGEST}, b"")


def fake_git(args, **kwargs):
    assert args[:2] == ["git", "ls-remote"]
    return subprocess.CompletedProcess(args, 0, f"{RELEASE_SHA}\trefs/tags/v0.0.45\n", "")


def test_resolve_image_digest_uses_the_anonymous_pull_token() -> None:
    registry = FakeRegistry()
    digest = resolve_image_digest(image="owner/app", reference="v0.0.45", transport=registry)
    assert digest == RELEASE_DIGEST
    assert registry.calls[0][1] == "https://ghcr.io/token?scope=repository:owner/app:pull"
    method, url, headers = registry.calls[1]
    assert (method, url) == ("HEAD", "https://ghcr.io/v2/owner/app/manifests/v0.0.45")
    assert headers["Authorization"] == "Bearer anon"
    assert "application/vnd.oci.image.index.v1+json" in headers["Accept"]


def test_resolve_image_digest_failures_are_explicit() -> None:
    with pytest.raises(ReleaseError, match="does not exist in the registry"):
        resolve_image_digest(
            image="owner/app", reference="nope", transport=FakeRegistry(manifest_status=404)
        )
    with pytest.raises(ReleaseError, match="token request failed"):
        resolve_image_digest(
            image="owner/app", reference="v1", transport=FakeRegistry(token_status=403)
        )
    with pytest.raises(ValueError, match="owner/name"):
        resolve_image_digest(image="Bad Image", reference="v1", transport=FakeRegistry())


def test_resolve_release_identity_combines_git_and_registry() -> None:
    identity = resolve_release_identity(
        repo="https://github.com/owner/app.git",
        image="owner/app",
        tag="v0.0.45",
        runner=fake_git,
        transport=FakeRegistry(),
    )
    assert identity == ReleaseIdentity(
        "v0.0.45", RELEASE_SHA, "ghcr.io", "owner/app", RELEASE_DIGEST
    )
    assert identity.image_ref == f"ghcr.io/owner/app@{RELEASE_DIGEST}"
    assert identity.to_dict()["image_ref"] == identity.image_ref
    with pytest.raises(ReleaseError, match="not a tag"):
        resolve_release_identity(
            repo="https://github.com/owner/app.git",
            image="owner/app",
            tag=RELEASE_SHA,
            runner=fake_git,
            transport=FakeRegistry(),
        )


def test_verify_runtime_identity_compares_only_what_the_release_sets() -> None:
    expected = RuntimeIdentity(
        "app", "v0.0.45", "production", RELEASE_SHA, image_digest=RELEASE_DIGEST
    )
    same = RuntimeIdentity(
        "app", "v0.0.45", "production", RELEASE_SHA, image_digest=RELEASE_DIGEST, release_id="r1"
    )
    assert verify_runtime_identity(expected, same) == ()
    other = RuntimeIdentity(
        "app", "v0.0.44", "production", "unknown", image_digest="sha256:" + "0" * 64
    )
    assert verify_runtime_identity(expected, other) == (
        "service_version",
        "commit_sha",
        "image_digest",
    )
    loose = RuntimeIdentity("app", "v0.0.45", "production", "unknown")
    assert verify_runtime_identity(loose, other) == ("service_version",)


def test_resolve_image_digest_passes_a_digest_through_and_rejects_odd_references() -> None:
    assert (
        resolve_image_digest(image="owner/app", reference=RELEASE_DIGEST, transport=FakeRegistry())
        == RELEASE_DIGEST
    )
    with pytest.raises(ValueError, match="registry tag or a sha256 digest"):
        resolve_image_digest(image="owner/app", reference="v1 nope", transport=FakeRegistry())


def test_resolve_image_digest_answers_a_bearer_challenge_from_any_registry() -> None:
    calls: list[tuple[str, str, dict]] = []

    def transport(method, url, headers, body):
        calls.append((method, url, dict(headers)))
        if url.startswith("https://auth.example/token"):
            return HttpResponse(200, {}, json.dumps({"access_token": "granted"}).encode())
        if "Authorization" not in headers:
            return HttpResponse(
                401,
                {
                    "WWW-Authenticate": 'Bearer realm="https://auth.example/token",service="reg",scope="repository:owner/app:pull"'
                },
                b"",
            )
        return HttpResponse(200, {"Docker-Content-Digest": RELEASE_DIGEST}, b"")

    digest = resolve_image_digest(
        image="owner/app", reference="v1", registry="registry.example", transport=transport
    )
    assert digest == RELEASE_DIGEST
    methods = [(m, u.split("/")[2]) for m, u, _ in calls]
    assert methods == [
        ("HEAD", "registry.example"),
        ("GET", "auth.example"),
        ("HEAD", "registry.example"),
    ]
    assert "scope=repository%3Aowner%2Fapp%3Apull" in calls[1][1]
    assert calls[2][2]["Authorization"] == "Bearer granted"
