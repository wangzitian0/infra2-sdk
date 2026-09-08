import json
import subprocess

import pytest

from infra2_sdk._transport import HttpResponse
from infra2_sdk.release import (
    ReleaseError,
    ReleaseIdentity,
    resolve_image_digest,
    resolve_release_identity,
    verify_runtime_identity,
)
from infra2_sdk.runtime.identity import RuntimeIdentity

SHA = "0123456789abcdef0123456789abcdef01234567"
DIGEST = "sha256:" + "f" * 64


class FakeRegistry:
    def __init__(self, *, manifest_status: int = 200, token_status: int = 200) -> None:
        self.manifest_status = manifest_status
        self.token_status = token_status
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def __call__(self, method, url, headers, body) -> HttpResponse:
        self.calls.append((method, url, dict(headers)))
        if "/token?" in url:
            return HttpResponse(self.token_status, {}, json.dumps({"token": "anon"}).encode())
        return HttpResponse(self.manifest_status, {"docker-content-digest": DIGEST}, b"")


def fake_git(args, **kwargs):
    assert args[:2] == ["git", "ls-remote"]
    return subprocess.CompletedProcess(args, 0, f"{SHA}\trefs/tags/v0.0.45\n", "")


def test_resolve_image_digest_uses_the_anonymous_pull_token() -> None:
    registry = FakeRegistry()
    digest = resolve_image_digest(image="owner/app", reference="v0.0.45", transport=registry)
    assert digest == DIGEST
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
    assert identity == ReleaseIdentity("v0.0.45", SHA, "ghcr.io", "owner/app", DIGEST)
    assert identity.image_ref == f"ghcr.io/owner/app@{DIGEST}"
    assert identity.to_dict()["image_ref"] == identity.image_ref
    with pytest.raises(ReleaseError, match="not a tag"):
        resolve_release_identity(
            repo="https://github.com/owner/app.git",
            image="owner/app",
            tag=SHA,
            runner=fake_git,
            transport=FakeRegistry(),
        )


def test_verify_runtime_identity_compares_only_what_the_release_sets() -> None:
    expected = RuntimeIdentity("app", "v0.0.45", "production", SHA, image_digest=DIGEST)
    same = RuntimeIdentity(
        "app", "v0.0.45", "production", SHA, image_digest=DIGEST, release_id="r1"
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
        resolve_image_digest(image="owner/app", reference=DIGEST, transport=FakeRegistry())
        == DIGEST
    )
    with pytest.raises(ValueError, match="registry tag or a sha256 digest"):
        resolve_image_digest(image="owner/app", reference="v1 nope", transport=FakeRegistry())


def test_resolve_image_digest_answers_a_bearer_challenge_from_any_registry() -> None:
    """A non-GHCR registry (or a private package) answers 401 with the realm to ask."""
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
        return HttpResponse(200, {"Docker-Content-Digest": DIGEST}, b"")

    digest = resolve_image_digest(
        image="owner/app", reference="v1", registry="registry.example", transport=transport
    )
    assert digest == DIGEST
    methods = [(m, u.split("/")[2]) for m, u, _ in calls]
    assert methods == [
        ("HEAD", "registry.example"),
        ("GET", "auth.example"),
        ("HEAD", "registry.example"),
    ]
    assert "scope=repository%3Aowner%2Fapp%3Apull" in calls[1][1]
    assert calls[2][2]["Authorization"] == "Bearer granted"
