"""Side-effect-free Git ref classification with injectable remote resolution."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Protocol

_SHA_RE = re.compile(r"\A[0-9a-fA-F]{7,40}\Z")
_TAG_RE = re.compile(r"\Av\d+\.\d+\.\d+\Z")
_LS_REMOTE_TIMEOUT_SECONDS = 30


class CommandRunner(Protocol):
    def __call__(self, *args, **kwargs): ...


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
            f"{_redact_repo(repo)}"
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
            raise ValueError(f"tag {cleaned!r} not found in {_redact_repo(repo)}")
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
    for sha, name in _ls_remote_rows(repo, remote_ref, runner=runner):
        if name == remote_ref:
            return ResolvedRef(sha=sha, image_ref=sha[:7], form="pr")
    raise ValueError(f"PR #{number} head not found in {_redact_repo(repo)}")


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
    rows = _ls_remote_rows(repo, *query, runner=runner)
    if peeled:
        for sha, name in rows:
            if name == peeled:
                return sha
    for sha, name in rows:
        if name == remote_ref:
            return sha
    return None


def _ls_remote_rows(
    repo: str,
    *remote_refs: str,
    runner: CommandRunner,
) -> list[tuple[str, str]]:
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
            f"git ls-remote failed for {list(remote_refs)!r} in {_redact_repo(repo)}: "
            f"{_redact_repo(str(exc))}"
        ) from None
    rows: list[tuple[str, str]] = []
    for line in (result.stdout or "").strip().splitlines():
        sha, _, name = line.partition("\t")
        if sha.strip() and name.strip():
            rows.append((sha.strip(), name.strip()))
    return rows


def _redact_repo(repo: str) -> str:
    return re.sub(r"(://)[^/@\s]+@", r"\1<redacted>@", repo)
