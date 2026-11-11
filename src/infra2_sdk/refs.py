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
_SHORT_SHA_LENGTH = 7
_FULL_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_OCI_DIGEST_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_REFERENCE_RE = re.compile(r"\A[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")
_IMAGE_RE = re.compile(r"\A[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+\Z")
_MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)


@runtime_checkable
class _CommandRunner(Protocol):
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


class ReleaseError(RuntimeError):
    pass


def classify_ref(ref: str) -> str:
    c = ref.strip()
    if not c:
        raise ValueError("deploy ref must be non-empty")
    if _TAG_RE.match(c):
        return "tag"
    if c == "main":
        return "branch"
    if _SHA_RE.match(c):
        return "sha"
    raise ValueError(f"unrecognized deploy ref {ref!r}: expected 'main', 'vX.Y.Z', or a commit sha")


def identifier_kind(value: str) -> str:
    if not value or value == "unknown":
        return "unset"
    if _SHA_RE.match(value) and value == value.lower():
        return "commit sha"
    return "release tag" if _TAG_RE.match(value) else "unrecognised"


def resolve_to_sha(ref: str, *, repo: str, runner: _CommandRunner = subprocess.run) -> str:
    form, c = classify_ref(ref), ref.strip()
    if form == "sha":
        return c.lower()
    if sha := _resolve_remote_sha(repo, form, c, runner=runner):
        return sha
    raise ValueError(
        f"deploy ref {ref!r} ({_remote_ref_for(form, c)}) not found in {_redact_repo(repo)}"
    )


def resolve_image_ref(
    ref: str, *, repo: str, runner: _CommandRunner = subprocess.run
) -> ResolvedRef:
    form, c = classify_ref(ref), ref.strip()
    if form == "tag":
        if not (sha := _resolve_remote_sha(repo, form, c, runner=runner)):
            raise ValueError(f"tag {c!r} not found in {_redact_repo(repo)}")
        return ResolvedRef(sha, c, form)
    sha = resolve_to_sha(ref, repo=repo, runner=runner)
    return ResolvedRef(sha, sha[:_SHORT_SHA_LENGTH], form)


def resolve_pr(
    pr_number: int | str, *, repo: str, runner: _CommandRunner = subprocess.run
) -> ResolvedRef:
    num = str(pr_number).strip()
    if not (num.isdigit() and int(num) > 0):
        raise ValueError(f"PR number must be a positive integer, got {pr_number!r}")
    rref = f"refs/pull/{num}/head"
    for sha, name in _ls_remote_rows(repo, rref, runner=runner):
        if name == rref:
            return ResolvedRef(sha, sha[:_SHORT_SHA_LENGTH], "pr")
    raise ValueError(f"PR #{num} head not found in {_redact_repo(repo)}")


def _remote_ref_for(form: str, cleaned: str) -> str:
    return "refs/heads/main" if form == "branch" else f"refs/tags/{cleaned}"


def _resolve_remote_sha(
    repo: str, form: str, cleaned: str, *, runner: _CommandRunner
) -> str | None:
    rref = _remote_ref_for(form, cleaned)
    peeled = f"{rref}^{{}}" if form == "tag" else None
    rows = dict(
        (name, sha)
        for sha, name in _ls_remote_rows(repo, *(r for r in (rref, peeled) if r), runner=runner)
    )
    return (rows.get(peeled) if peeled else None) or rows.get(rref)


def _ls_remote_rows(
    repo: str, *remote_refs: str, runner: _CommandRunner = subprocess.run
) -> list[tuple[str, str]]:
    try:
        res = runner(
            ["git", "ls-remote", repo, *remote_refs],
            capture_output=True,
            text=True,
            check=True,
            timeout=_LS_REMOTE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(
            f"git ls-remote failed for {list(remote_refs)!r} in {_redact_repo(repo)}: "
            f"{_redact_repo(str(exc))}"
        ) from None
    return [
        (sha.strip(), name.strip())
        for line in (res.stdout or "").strip().splitlines()
        if (sha := line.partition("\t")[0]).strip() and (name := line.partition("\t")[2]).strip()
    ]


def _redact_repo(repo: str) -> str:
    return re.sub(r"(://)[^/@\s]+@", r"\1<redacted>@", repo)


def resolve_image_digest(
    *,
    image: str,
    reference: str,
    registry: str = "ghcr.io",
    token: str | None = None,
    transport: HttpTransport | None = None,
) -> str:
    if _OCI_DIGEST_RE.match(reference):
        return reference
    if not _IMAGE_RE.match(image):
        raise ValueError("image must look like owner/name")
    if not _REFERENCE_RE.match(reference):
        raise ValueError(f"reference must be a registry tag or a sha256 digest, got {reference!r}")
    send = transport or urllib_transport()
    if token is None and registry == "ghcr.io":
        r = send("GET", f"https://{registry}/token?scope=repository:{image}:pull", {}, None)
        if r.status != 200:
            raise ReleaseError(f"registry token request failed with HTTP {r.status}")
        try:
            token = str(json.loads(r.body.decode("utf-8")).get("token", ""))
        except ValueError as err:
            raise ReleaseError("registry token response is not JSON") from err
    url = f"https://{registry}/v2/{image}/manifests/{reference}"

    def head(bearer: str | None) -> HttpResponse:
        h = {"Accept": _MANIFEST_ACCEPT}
        if bearer:
            h["Authorization"] = f"Bearer {bearer}"
        return send("HEAD", url, h, None)

    r = head(token)
    if r.status == 401:
        r = head(_bearer_token(send, _header(r.headers, "www-authenticate")))
    if r.status == 404:
        raise ReleaseError(f"{image}:{reference} does not exist in the registry")
    if r.status in (401, 403):
        raise ReleaseError(f"registry refused {image}:{reference} (HTTP {r.status})")
    if not 200 <= r.status < 300:
        raise ReleaseError(f"registry answered HTTP {r.status} for {image}:{reference}")
    digest = _header(r.headers, "docker-content-digest")
    if not _OCI_DIGEST_RE.match(digest):
        raise ReleaseError("registry returned no sha256 content digest")
    return digest


def _header(headers: Mapping[str, str], name: str) -> str:
    return next((str(v) for k, v in headers.items() if k.lower() == name), "")


def _parse_bearer_challenge(header: str) -> dict[str, str]:
    scheme, _, params = header.partition(" ")
    if scheme.lower() != "bearer":
        return {}
    return {
        k.strip(): v.strip().strip('"')
        for k, _, v in (p.partition("=") for p in params.split(","))
        if k.strip()
    }


def _bearer_token(send: HttpTransport, challenge: str) -> str:
    params = _parse_bearer_challenge(challenge)
    if not (realm := params.get("realm", "")):
        raise ReleaseError("registry challenged without a bearer realm")
    q = urlencode({k: v for k, v in params.items() if k in ("service", "scope")})
    r = send("GET", f"{realm}?{q}" if q else realm, {}, None)
    if r.status != 200:
        raise ReleaseError(f"registry token endpoint answered HTTP {r.status}")
    try:
        body = json.loads(r.body.decode("utf-8"))
    except ValueError as err:
        raise ReleaseError("registry token response is not JSON") from err
    if not (tok := str(body.get("token") or body.get("access_token") or "")):
        raise ReleaseError("registry token endpoint returned no token")
    return tok


def short_sha(sha: str) -> str:
    if not _FULL_SHA_RE.fullmatch(sha):
        raise ValueError(f"--sha must be the full 40-hex commit sha the tag points at, got {sha!r}")
    return sha[:_SHORT_SHA_LENGTH]


def next_patch_tag(tags: Any, *, initial: str = "v0.1.0") -> str:
    if not _TAG_RE.match(initial):
        raise ValueError(f"initial must match vX.Y.Z, got {initial!r}")
    vers = [
        tuple(int(p) for p in c[1:].split("."))
        for raw in tags
        if isinstance(raw, str) and _TAG_RE.match(c := raw.strip().removeprefix("refs/tags/"))
    ]
    if not vers:
        return initial
    maj, min_, pat = max(vers)
    return f"v{maj}.{min_}.{pat + 1}"


def assert_after_on_main(
    after: str,
    repo_root: Any,
    *,
    base: str = "origin/main",
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> str:
    def git(subcmd: str, *args: str) -> subprocess.CompletedProcess:
        try:
            return runner(
                ["git", subcmd, *args],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=_LS_REMOTE_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ReleaseError(f"git {subcmd} failed for {after!r}: {exc}") from exc

    res = git("rev-parse", "--verify", f"{after}^{{commit}}")
    if res.returncode != 0:
        raise SystemExit(
            f"::error::cannot resolve {after!r} to a commit "
            f"({res.stderr.strip() or 'unknown revision'})."
        )
    sha = res.stdout.strip()
    anc = git("merge-base", "--is-ancestor", sha, base)
    if anc.returncode == 0:
        return sha
    if anc.returncode == 1:
        raise SystemExit(
            f"::error::refusing to reconcile {after!r} ({sha[:12]}): not reachable from "
            f"{base}. Release tags must be cut on reviewed main (Infra-011 invariant). "
            f"Re-cut the tag on main, or pass --dry-run to plan only."
        )
    raise SystemExit(
        f"::error::cannot verify provenance of {after!r}: base ref {base!r} is "
        f"unresolvable ({anc.stderr.strip() or 'git error'}). Ensure it exists, e.g. "
        f"`git fetch --no-tags origin +refs/heads/main:refs/remotes/origin/main`."
    )


__all__ = [
    "ReleaseError",
    "ResolvedRef",
    "assert_after_on_main",
    "classify_ref",
    "identifier_kind",
    "next_patch_tag",
    "resolve_image_digest",
    "resolve_image_ref",
    "resolve_pr",
    "resolve_to_sha",
    "short_sha",
]
