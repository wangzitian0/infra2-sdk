import subprocess
from types import SimpleNamespace

import pytest

from infra2_sdk.refs import classify_ref, resolve_image_ref, resolve_pr, resolve_to_sha

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
