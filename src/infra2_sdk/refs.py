"""Side-effect-free Git ref classification with injectable remote resolution."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlencode

from infra2_sdk.transport import HttpResponse, HttpTransport, urllib_transport

_SHA_RE = re.compile(r"\A[0-9a-fA-F]{7,40}\Z")
_TAG_RE = re.compile(r"\Av\d+\.\d+\.\d+\Z")
_LS_REMOTE_TIMEOUT_SECONDS = 30


@runtime_checkable
class CommandRunner(Protocol):
    """Callable protocol for command execution (compatible with subprocess.run)."""

    def __call__(
        self,
        args: list[str],
        *,
        capture_output: bool = True,
        text: bool = True,
        check: bool = True,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> Any: ...


@dataclass(frozen=True)
class ResolvedRef:
    sha: str
    image_ref: str
    form: str


def classify_ref(ref: str) -> str:
    cleaned = ref.strip()
    if not cleaned:
        raise ValueError("deploy ref must be non-empty")
    if _TAG_RE.match(cleaned):
        return "tag"
    if cleaned == "main":
        return "branch"
    if _SHA_RE.match(cleaned):
        return "sha"
    raise ValueError(f"unrecognized deploy ref {ref!r}: expected 'main', 'vX.Y.Z', or a commit sha")


def resolve_to_sha(
    ref: str,
    *,
    repo: str,
    runner: CommandRunner = subprocess.run,
) -> str:
    form = classify_ref(ref)
    cleaned = ref.strip()
    if form == "sha":
        return cleaned.lower()
    sha = _resolve_remote_sha(repo, form, cleaned, runner=runner)
    if not sha:
        raise ValueError(
            f"deploy ref {ref!r} ({_remote_ref_for(form, cleaned)}) not found in "
            f"{redact_repo(repo)}"
        )
    return sha


def resolve_image_ref(
    ref: str,
    *,
    repo: str,
    runner: CommandRunner = subprocess.run,
) -> ResolvedRef:
    form = classify_ref(ref)
    cleaned = ref.strip()
    if form == "tag":
        sha = _resolve_remote_sha(repo, form, cleaned, runner=runner)
        if not sha:
            raise ValueError(f"tag {cleaned!r} not found in {redact_repo(repo)}")
        return ResolvedRef(sha=sha, image_ref=cleaned, form=form)
    sha = resolve_to_sha(ref, repo=repo, runner=runner)
    return ResolvedRef(sha=sha, image_ref=sha[:7], form=form)


def resolve_pr(
    pr_number: int | str,
    *,
    repo: str,
    runner: CommandRunner = subprocess.run,
) -> ResolvedRef:
    number = str(pr_number).strip()
    if not (number.isdigit() and int(number) > 0):
        raise ValueError(f"PR number must be a positive integer, got {pr_number!r}")
    remote_ref = f"refs/pull/{number}/head"
    for sha, name in ls_remote_rows(repo, remote_ref, runner=runner):
        if name == remote_ref:
            return ResolvedRef(sha=sha, image_ref=sha[:7], form="pr")
    raise ValueError(f"PR #{number} head not found in {redact_repo(repo)}")


def resolve_branch_to_sha(
    branch: str,
    *,
    repo: str,
    runner: CommandRunner = subprocess.run,
) -> str:
    """Resolve a clone-only branch without broadening the deploy authority grammar."""
    cleaned = branch.strip()
    invalid_chars = " ~^:?*[\x5c"
    if (
        not cleaned
        or cleaned.startswith(("-", ".", "/", "refs/"))
        or cleaned.endswith((".", "/", ".lock"))
        or ".." in cleaned
        or "@{" in cleaned
        or "//" in cleaned
        or any(ord(char) < 32 or char in invalid_chars for char in cleaned)
    ):
        raise ValueError(f"invalid clone branch {branch!r}")

    remote_ref = f"refs/heads/{cleaned}"
    for sha, name in ls_remote_rows(repo, remote_ref, runner=runner):
        if name == remote_ref:
            return sha
    raise ValueError(f"clone branch {branch!r} ({remote_ref}) not found in {redact_repo(repo)}")


def _remote_ref_for(form: str, cleaned: str) -> str:
    return {"branch": "refs/heads/main", "tag": f"refs/tags/{cleaned}"}[form]


def _resolve_remote_sha(
    repo: str,
    form: str,
    cleaned: str,
    *,
    runner: CommandRunner,
) -> str | None:
    remote_ref = _remote_ref_for(form, cleaned)
    peeled = remote_ref + "^{}" if form == "tag" else None
    query = [remote_ref, peeled] if peeled else [remote_ref]
    rows = ls_remote_rows(repo, *query, runner=runner)
    if peeled:
        for sha, name in rows:
            if name == peeled:
                return sha
    for sha, name in rows:
        if name == remote_ref:
            return sha
    return None


def ls_remote_rows(
    repo: str,
    *remote_refs: str,
    runner: CommandRunner = subprocess.run,
) -> list[tuple[str, str]]:
    """Run ``git ls-remote repo *remote_refs`` and return ``(sha, ref name)`` rows.

    ``runner`` is injectable (any ``subprocess.run``-compatible callable). A failing command,
    a missing ``git``, or a timeout raises ``ValueError`` whose message has credentials
    embedded in the repository URL redacted by ``redact_repo``.
    """

    try:
        result = runner(
            ["git", "ls-remote", repo, *remote_refs],
            capture_output=True,
            text=True,
            check=True,
            timeout=_LS_REMOTE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(
            f"git ls-remote failed for {list(remote_refs)!r} in {redact_repo(repo)}: "
            f"{redact_repo(str(exc))}"
        ) from None
    rows: list[tuple[str, str]] = []
    for line in (result.stdout or "").strip().splitlines():
        sha, _, name = line.partition("\t")
        if sha.strip() and name.strip():
            rows.append((sha.strip(), name.strip()))
    return rows


def redact_repo(repo: str) -> str:
    """Replace credentials embedded in a URL (``scheme://user:token@host``) with ``<redacted>``."""

    return re.sub(r"(://)[^/@\s]+@", r"\1<redacted>@", repo)


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


def resolve_image_digest(
    *,
    image: str,
    reference: str,
    registry: str = "ghcr.io",
    token: str | None = None,
    transport: HttpTransport | None = None,
) -> str:
    """Digest of ``registry/image:reference`` from the registry's manifest endpoint."""
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


SHORT_SHA_LENGTH = 7
_FULL_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")


def short_sha(sha: str) -> str:
    """Return the 7-character short commit SHA after validating a 40-hex commit SHA."""
    if not _FULL_SHA_RE.fullmatch(sha):
        raise ValueError(f"--sha must be the full 40-hex commit sha the tag points at, got {sha!r}")
    return sha[:SHORT_SHA_LENGTH]


def assert_after_on_main(
    after: str,
    repo_root: Any,
    *,
    base: str = "origin/main",
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> str:
    """Fail-closed provenance guard: the promoted tag or ref must be reachable from main."""
    resolved = runner(
        ["git", "rev-parse", "--verify", f"{after}^{{commit}}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if resolved.returncode != 0:
        raise SystemExit(
            f"::error::cannot resolve {after!r} to a commit "
            f"({resolved.stderr.strip() or 'unknown revision'})."
        )
    sha = resolved.stdout.strip()
    ancestor = runner(
        ["git", "merge-base", "--is-ancestor", sha, base],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if ancestor.returncode == 0:
        return sha
    if ancestor.returncode == 1:
        raise SystemExit(
            f"::error::refusing to reconcile {after!r} ({sha[:12]}): not reachable from "
            f"{base}. Release tags must be cut on reviewed main (Infra-011 invariant). "
            f"Re-cut the tag on main, or pass --dry-run to plan only."
        )
    raise SystemExit(
        f"::error::cannot verify provenance of {after!r}: base ref {base!r} is "
        f"unresolvable ({ancestor.stderr.strip() or 'git error'}). Ensure it exists, e.g. "
        f"`git fetch --no-tags origin +refs/heads/main:refs/remotes/origin/main`."
    )


__all__ = [
    "CommandRunner",
    "MANIFEST_ACCEPT",
    "ReleaseError",
    "ResolvedRef",
    "SHORT_SHA_LENGTH",
    "assert_after_on_main",
    "classify_ref",
    "ls_remote_rows",
    "redact_repo",
    "resolve_branch_to_sha",
    "resolve_image_digest",
    "resolve_image_ref",
    "resolve_pr",
    "resolve_to_sha",
    "short_sha",
]
