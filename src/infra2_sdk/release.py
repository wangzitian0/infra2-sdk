"""Release identity: what a tag resolves to, and whether a runtime reports the same thing.

A release request carries a tag. The commit is what the repository says the tag points
at; the image digest is what the registry says the tag's manifest is. Neither is stored
anywhere: both are re-derived on every deployment and compared with what the running
artifact reports.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from urllib.parse import urlencode

from infra2_sdk._transport import HttpResponse, HttpTransport, urllib_transport
from infra2_sdk.refs import CommandRunner, resolve_image_ref
from infra2_sdk.runtime.identity import RuntimeIdentity

_OCI_DIGEST_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_REFERENCE_RE = re.compile(r"\A[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")
_IMAGE_RE = re.compile(r"\A[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+\Z")
MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)


class ReleaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReleaseIdentity:
    tag: str
    commit_sha: str
    registry: str
    image: str
    image_digest: str

    @property
    def image_ref(self) -> str:
        return f"{self.registry}/{self.image}@{self.image_digest}"

    def to_dict(self) -> dict[str, str]:
        data = asdict(self)
        data["image_ref"] = self.image_ref
        return data


def resolve_image_digest(
    *,
    image: str,
    reference: str,
    registry: str = "ghcr.io",
    token: str | None = None,
    transport: HttpTransport | None = None,
) -> str:
    """Digest of ``registry/image:reference`` from the registry's manifest endpoint.

    ``reference`` is a tag, or already a ``sha256:`` digest (returned unchanged after a
    shape check, so a digest-pinned deploy re-verifies nothing it does not need to).
    Public GHCR packages need only the anonymous pull token the registry itself issues;
    any registry that answers 401 with a Bearer challenge gets its token from the realm
    the challenge names. Fails closed on 404 (no such tag), on 401/403 after the
    challenge, and on every other non-2xx: a promotion never guesses a digest.
    """
    if _OCI_DIGEST_RE.match(reference):
        return reference
    if not _IMAGE_RE.match(image):
        raise ValueError("image must look like owner/name")
    if not _REFERENCE_RE.match(reference):
        raise ValueError(f"reference must be a registry tag or a sha256 digest, got {reference!r}")
    send = transport or urllib_transport()
    if token is None and registry == "ghcr.io":
        response = send("GET", f"https://{registry}/token?scope=repository:{image}:pull", {}, None)
        if response.status != 200:
            raise ReleaseError(f"registry token request failed with HTTP {response.status}")
        try:
            token = str(json.loads(response.body.decode("utf-8")).get("token", ""))
        except ValueError as error:
            raise ReleaseError("registry token response is not JSON") from error
    url = f"https://{registry}/v2/{image}/manifests/{reference}"

    def head(bearer: str | None) -> HttpResponse:
        headers = {"Accept": MANIFEST_ACCEPT}
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        return send("HEAD", url, headers, None)

    response = head(token)
    if response.status == 401:
        response = head(_bearer_token(send, _header(response.headers, "www-authenticate")))
    if response.status == 404:
        raise ReleaseError(f"{image}:{reference} does not exist in the registry")
    if response.status in (401, 403):
        raise ReleaseError(f"registry refused {image}:{reference} (HTTP {response.status})")
    if not 200 <= response.status < 300:
        raise ReleaseError(f"registry answered HTTP {response.status} for {image}:{reference}")
    digest = _header(response.headers, "docker-content-digest")
    if not _OCI_DIGEST_RE.match(digest):
        raise ReleaseError("registry returned no sha256 content digest")
    return digest


def _header(headers: Mapping[str, str], name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name:
            return str(value)
    return ""


def _parse_bearer_challenge(header: str) -> dict[str, str]:
    scheme, _, params = header.partition(" ")
    if scheme.lower() != "bearer":
        return {}
    return {
        key.strip(): value.strip().strip('"')
        for key, _, value in (part.partition("=") for part in params.split(","))
        if key.strip()
    }


def _bearer_token(send: HttpTransport, challenge: str) -> str:
    params = _parse_bearer_challenge(challenge)
    realm = params.get("realm", "")
    if not realm:
        raise ReleaseError("registry challenged without a bearer realm")
    query = urlencode({k: v for k, v in params.items() if k in ("service", "scope")})
    response = send("GET", f"{realm}?{query}" if query else realm, {}, None)
    if response.status != 200:
        raise ReleaseError(f"registry token endpoint answered HTTP {response.status}")
    try:
        body = json.loads(response.body.decode("utf-8"))
    except ValueError as error:
        raise ReleaseError("registry token response is not JSON") from error
    token = str(body.get("token") or body.get("access_token") or "")
    if not token:
        raise ReleaseError("registry token endpoint returned no token")
    return token


def resolve_release_identity(
    *,
    repo: str,
    image: str,
    tag: str,
    registry: str = "ghcr.io",
    runner: CommandRunner = subprocess.run,
    transport: HttpTransport | None = None,
) -> ReleaseIdentity:
    """Tag → commit (git ls-remote) and tag → digest (registry), in one immutable record."""

    resolved = resolve_image_ref(tag, repo=repo, runner=runner)
    if resolved.form != "tag":
        raise ReleaseError(f"{tag!r} is not a tag")
    digest = resolve_image_digest(
        image=image, reference=tag, registry=registry, transport=transport
    )
    return ReleaseIdentity(
        tag=tag.strip(),
        commit_sha=resolved.sha,
        registry=registry,
        image=image,
        image_digest=digest,
    )


def verify_runtime_identity(
    expected: RuntimeIdentity, observed: RuntimeIdentity
) -> tuple[str, ...]:
    """Names of identity coordinates where the running artifact disagrees with the release.

    Only coordinates the release sets are compared; an empty tuple means the deployment
    landed. Compare against the request, never against a store's current value.
    """

    mismatched: list[str] = []
    for name in (
        "service_version",
        "commit_sha",
        "image_digest",
        "release_id",
        "configuration_sha256",
    ):
        wanted = getattr(expected, name)
        if wanted and wanted != "unknown" and getattr(observed, name) != wanted:
            mismatched.append(name)
    return tuple(mismatched)
