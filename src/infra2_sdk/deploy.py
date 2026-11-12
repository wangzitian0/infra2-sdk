"""Versioned wire contract between an application and the infra2 deploy receiver."""

from __future__ import annotations

import argparse
import binascii
import json
import os
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from urllib.parse import urlparse

from infra2_sdk._wire import (
    canonical_json_bytes,
    parse_contract_version,
    parse_iso_utc,
    parse_string,
    require_contract_version,
)
from infra2_sdk.transport import (
    _decode_github_contents,
    _github_headers,
    _github_json_fetcher,
)

CONTRACT_VERSION = 1
_REQUEST_ID_RE = re.compile(r"\A[a-zA-Z0-9][a-zA-Z0-9._:-]{7,127}\Z")
_SERVICE_RE = re.compile(r"\A[a-z][a-z0-9_]*/[a-z][a-z0-9_]*\Z")
_REPOSITORY_RE = re.compile(r"\A[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+\Z")
_SHA40_RE = re.compile(r"\A[0-9a-f]{40}\Z")


class DeployOperation(StrEnum):
    DEPLOY = "deploy"
    ROLLBACK = "rollback"
    REMOVE = "remove"


class DeployType(StrEnum):
    STAGING = "staging"
    PRODUCTION = "prod"
    PREVIEW_BRANCH = "preview/branch"
    PREVIEW_PR = "preview/pr"
    PREVIEW_COMMIT = "preview/commit"
    PREVIEW_TAG = "preview/tag"
    CANARY = "canary"


PRODUCTION_REQUEST_MAX_AGE_SECONDS = 24 * 60
PRODUCTION_DEPLOY_BUDGET_SECONDS = 300
SENDER_MAX_WAIT_SECONDS = 6 * 60 * 60
SENDER_DEADLINE_KEY = "sender_deadline"


def parse_sender_deadline(payload: Mapping[str, Any]) -> datetime | None:
    val = payload.get(SENDER_DEADLINE_KEY)
    if val is None:
        return None
    if not isinstance(val, str):
        raise ValueError(f"{SENDER_DEADLINE_KEY} must be an ISO 8601 string")
    try:
        return parse_iso_utc(val)
    except ValueError:
        raise ValueError(
            f"{SENDER_DEADLINE_KEY} {val!r} is not an ISO 8601 time with a time zone"
        ) from None


def production_request_accept_until(
    created: datetime, sender_deadline: datetime | None
) -> datetime:
    if created.tzinfo is None:
        raise ValueError("the creation time must have a time zone")
    created = created.astimezone(UTC)
    if sender_deadline is None:
        return created + timedelta(seconds=PRODUCTION_REQUEST_MAX_AGE_SECONDS)
    if sender_deadline.tzinfo is None:
        raise ValueError("the sender deadline must have a time zone")
    latest = created + timedelta(seconds=SENDER_MAX_WAIT_SECONDS)
    return min(sender_deadline.astimezone(UTC), latest) - timedelta(
        seconds=PRODUCTION_DEPLOY_BUDGET_SECONDS
    )


@dataclass(frozen=True)
class DeployEvidence:
    source_run_url: str
    source_run_id: str = ""
    staging_run_url: str = ""
    reviewed_change_url: str = ""

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> DeployEvidence:
        return cls(
            source_run_url=parse_string(raw, "source_run_url"),
            source_run_id=parse_string(raw, "source_run_id", required=False),
            staging_run_url=parse_string(raw, "staging_run_url", required=False),
            reviewed_change_url=parse_string(raw, "reviewed_change_url", required=False),
        )


@dataclass(frozen=True)
class DeployRequest:
    request_id: str
    operation: DeployOperation
    service: str
    deploy_type: DeployType
    version_ref: str
    source_repository: str
    source_sha: str
    evidence: DeployEvidence
    contract_version: int = CONTRACT_VERSION

    def __post_init__(self) -> None:
        _validate_deploy_request(self)

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["operation"] = self.operation.value
        data["deploy_type"] = self.deploy_type.value
        return data

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> DeployRequest:
        ev = raw.get("evidence")
        if not isinstance(ev, Mapping):
            raise ValueError("evidence must be an object")
        ver = parse_contract_version(raw, CONTRACT_VERSION, description="contract_version")
        return cls(
            contract_version=ver,
            request_id=parse_string(raw, "request_id"),
            operation=DeployOperation(parse_string(raw, "operation")),
            service=parse_string(raw, "service"),
            deploy_type=DeployType(parse_string(raw, "deploy_type")),
            version_ref=parse_string(raw, "version_ref"),
            source_repository=parse_string(raw, "source_repository"),
            source_sha=parse_string(raw, "source_sha").lower(),
            evidence=DeployEvidence.from_dict(ev),
        )


def _validate_deploy_request(req: DeployRequest) -> None:
    require_contract_version(req.contract_version, CONTRACT_VERSION, description="contract_version")
    if not _REQUEST_ID_RE.match(req.request_id):
        raise ValueError("request_id must be 8-128 URL-safe characters")
    if not _SERVICE_RE.match(req.service):
        raise ValueError("service must have the form project/service")
    if not req.version_ref.strip():
        raise ValueError("version_ref is required")
    if not _REPOSITORY_RE.match(req.source_repository):
        raise ValueError("source_repository must have the form owner/repository")
    if not _SHA40_RE.match(req.source_sha):
        raise ValueError("source_sha must be a lowercase 40-hex commit sha")
    if not req.evidence.source_run_url.startswith("https://github.com/"):
        raise ValueError("evidence.source_run_url must be a GitHub URL")
    if req.deploy_type == DeployType.PRODUCTION and not (
        req.evidence.staging_run_url and req.evidence.reviewed_change_url
    ):
        raise ValueError("production deploys require staging and reviewed-change evidence")
    if req.operation == DeployOperation.REMOVE and req.deploy_type not in {
        DeployType.PREVIEW_BRANCH,
        DeployType.PREVIEW_PR,
        DeployType.PREVIEW_COMMIT,
        DeployType.PREVIEW_TAG,
        DeployType.CANARY,
    }:
        raise ValueError("remove is limited to preview and canary targets")


_EVIDENCE_POLICY_CONTRACT_VERSION = 1
PRODUCTION_EVIDENCE_POLICY_PATH = "tools/production_evidence_policy.json"
_WORKFLOW_PATH_RE = re.compile(r"\A\.github/workflows/[A-Za-z0-9._-]+\.ya?ml\Z")
_TITLE_PLACEHOLDER = "{version_ref}"
_RUN_EVENTS = frozenset({"push", "workflow_dispatch"})


@dataclass(frozen=True)
class _RunEvidenceExpectation:
    workflow_path: str
    event: str
    display_title_template: str
    require_head_sha: bool = True

    def __post_init__(self) -> None:
        if not _WORKFLOW_PATH_RE.match(self.workflow_path):
            raise ValueError("workflow_path must be a .github/workflows/<name>.yml path")
        if self.event not in _RUN_EVENTS:
            raise ValueError(f"event must be one of {sorted(_RUN_EVENTS)}")
        if not self.display_title_template.strip():
            raise ValueError("display_title_template is required")
        residue = self.display_title_template.replace(_TITLE_PLACEHOLDER, "")
        if "{" in residue or "}" in residue:
            raise ValueError(
                "display_title_template supports the literal {version_ref} placeholder only"
            )

    def expected_display_title(self, version_ref: str) -> str:
        return self.display_title_template.replace(_TITLE_PLACEHOLDER, version_ref)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> _RunEvidenceExpectation:
        req_sha = raw.get("require_head_sha", True)
        if type(req_sha) is not bool:
            raise ValueError("require_head_sha must be a boolean")
        return cls(
            workflow_path=parse_string(raw, "workflow_path"),
            event=parse_string(raw, "event"),
            display_title_template=parse_string(raw, "display_title_template"),
            require_head_sha=req_sha,
        )


@dataclass(frozen=True)
class ProductionEvidencePolicy:
    service: str
    source: _RunEvidenceExpectation
    staging: _RunEvidenceExpectation
    review_base_ref: str
    contract_version: int = _EVIDENCE_POLICY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        require_contract_version(
            self.contract_version,
            _EVIDENCE_POLICY_CONTRACT_VERSION,
            description="evidence policy contract_version",
        )
        if not _SERVICE_RE.match(self.service):
            raise ValueError("service must have the form project/service")
        if not self.review_base_ref.strip():
            raise ValueError("review_base_ref is required")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ProductionEvidencePolicy:
        ver = parse_contract_version(
            raw, _EVIDENCE_POLICY_CONTRACT_VERSION, description="evidence policy contract_version"
        )
        src, stg = raw.get("source"), raw.get("staging")
        if not isinstance(src, Mapping):
            raise ValueError("source must be an object")
        if not isinstance(stg, Mapping):
            raise ValueError("staging must be an object")
        return cls(
            contract_version=ver,
            service=parse_string(raw, "service"),
            source=_RunEvidenceExpectation.from_dict(src),
            staging=_RunEvidenceExpectation.from_dict(stg),
            review_base_ref=parse_string(raw, "review_base_ref"),
        )


def _default_github_json_fetcher(
    *,
    token: str = "",
    token_env: str = "GITHUB_TOKEN",
    user_agent: str = "infra2-sdk",
    timeout: float = 10.0,
) -> Callable[[str], Any]:
    return _github_json_fetcher(
        headers=_github_headers(token=token or os.getenv(token_env, ""), user_agent=user_agent),
        timeout=timeout,
        error_cls=ValueError,
        http_message="GitHub evidence request failed for {path}: HTTP {code}",
        error_message="GitHub evidence request failed for {path}: {error}",
        error_text=lambda exc: type(exc).__name__,
    )


def _require_github_path(url: str, *, prefix: str, field: str) -> None:
    p = urlparse(url)
    if p.scheme != "https" or p.netloc != "github.com" or not p.path.startswith(prefix):
        raise ValueError(f"evidence.{field} must point to {prefix} on github.com")


def _github_evidence_number(url: str, *, repository: str, resource: str, field: str) -> str:
    p = urlparse(url)
    prefix = f"/{repository}/{resource}/"
    num = p.path.removeprefix(prefix)
    if (
        p.scheme != "https"
        or p.netloc != "github.com"
        or not p.path.startswith(prefix)
        or not num.isdigit()
        or p.params
        or p.query
        or p.fragment
    ):
        raise ValueError(f"evidence.{field} must be a canonical github.com {resource} URL")
    return num


def validate_evidence_urls(
    request: DeployRequest, *, expected_repository: str | None = None
) -> None:
    if expected_repository is not None and expected_repository != request.source_repository:
        raise ValueError("source_repository does not match the expected repository")
    base = f"/{expected_repository or request.source_repository}/"
    _require_github_path(
        request.evidence.source_run_url, prefix=f"{base}actions/runs/", field="source_run_url"
    )
    if request.deploy_type == DeployType.PRODUCTION:
        _require_github_path(
            request.evidence.staging_run_url, prefix=f"{base}actions/runs/", field="staging_run_url"
        )
        _require_github_path(
            request.evidence.reviewed_change_url,
            prefix=f"{base}pull/",
            field="reviewed_change_url",
        )


_validate_evidence_urls = validate_evidence_urls


def _verify_run(
    run: Mapping[str, object],
    *,
    label: str,
    repository: str,
    url: str,
    sha: str | None,
    event: str,
    workflow_path: str,
    display_title: str,
) -> None:
    repo = run.get("repository")
    if not isinstance(repo, Mapping) or repo.get("full_name") != repository:
        raise ValueError(f"{label} repository does not match source_repository")
    if run.get("html_url") != url:
        raise ValueError(f"{label} html_url does not match submitted evidence")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise ValueError(f"{label} must be completed successfully")
    if sha is not None and run.get("head_sha") != sha:
        raise ValueError(f"{label} head_sha does not match source_sha")
    if run.get("path") != workflow_path:
        raise ValueError(f"{label} workflow is not approved for Production evidence")
    if run.get("event") != event:
        raise ValueError(f"{label} event does not match the evidence policy")
    if run.get("display_title") != display_title:
        raise ValueError(f"{label} title does not match the requested version_ref")


def _verify_reviewed_pull(
    pull: Mapping[str, object],
    *,
    reviews: Sequence[object] | None = None,
    repository: str,
    url: str,
    sha: str,
    base_ref: str,
) -> None:
    base = pull.get("base")
    repo = base.get("repo") if isinstance(base, Mapping) else None
    if not isinstance(repo, Mapping) or repo.get("full_name") != repository:
        raise ValueError("reviewed pull request repository does not match source_repository")
    if pull.get("html_url") != url:
        raise ValueError("reviewed pull request html_url does not match submitted evidence")
    if pull.get("state") != "closed" or not pull.get("merged_at"):
        raise ValueError("reviewed pull request must be merged")
    if not isinstance(base, Mapping) or base.get("ref") != base_ref:
        raise ValueError("reviewed pull request base branch is not approved")
    if pull.get("merge_commit_sha") != sha:
        raise ValueError("reviewed pull request merge_commit_sha does not match source_sha")
    if reviews is not None:
        user = pull.get("user")
        author = user.get("login") if isinstance(user, Mapping) else None
        states = {
            r["user"]["login"]: r["state"]
            for r in reviews
            if isinstance(r, Mapping)
            and isinstance(r.get("user"), Mapping)
            and isinstance(r["user"].get("login"), str)
            and r["user"]["login"]
            and isinstance(r.get("state"), str)
        }
        if author:
            states.pop(author, None)
        if any(s == "CHANGES_REQUESTED" for s in states.values()):
            raise ValueError("reviewed pull request has pending CHANGES_REQUESTED")


def _parse_evidence_policy_payload(
    where: str, payload: Any, *, service: str | None = None
) -> ProductionEvidencePolicy:
    try:
        raw = json.loads(_decode_github_contents(payload))
    except TypeError:
        raise ValueError(f"{where} must contain a JSON object") from None
    except KeyError:
        raise ValueError(f"{where} did not return file content") from None
    except (binascii.Error, ValueError) as exc:
        raise ValueError(
            f"malformed {PRODUCTION_EVIDENCE_POLICY_PATH}: not valid JSON ({exc})"
        ) from exc
    if not isinstance(raw, Mapping):
        raise ValueError(f"malformed {PRODUCTION_EVIDENCE_POLICY_PATH}: expected JSON object")
    try:
        pol = ProductionEvidencePolicy.from_dict(raw)
    except ValueError as exc:
        raise ValueError(f"{where} is not a valid evidence contract: {exc}") from exc
    if service is not None and pol.service != service:
        raise ValueError(f"{where} declares service {pol.service!r}, not {service!r}")
    return pol


def _fetch_production_evidence_policy(
    request: DeployRequest, *, fetch_json: Callable[[str], Any] | None = None
) -> ProductionEvidencePolicy:
    where = f"{request.source_repository}:{PRODUCTION_EVIDENCE_POLICY_PATH}@{request.source_sha}"
    fetch = fetch_json or _default_github_json_fetcher()
    try:
        payload = fetch(
            f"/repos/{request.source_repository}/contents/"
            f"{PRODUCTION_EVIDENCE_POLICY_PATH}?ref={request.source_sha}"
        )
    except Exception as exc:
        raise ValueError(
            f"service {request.service!r} has no Production evidence contract: "
            f"fetching {where} failed ({exc})."
        ) from exc
    return _parse_evidence_policy_payload(where, payload, service=request.service)


def verify_production_evidence(
    request: DeployRequest,
    policy: ProductionEvidencePolicy | None = None,
    *,
    fetch_json: Callable[[str], Any] | None = None,
) -> None:
    fetch = fetch_json or _default_github_json_fetcher()
    _validate_evidence_urls(request)
    repo = request.source_repository
    src_id = _github_evidence_number(
        request.evidence.source_run_url,
        repository=repo,
        resource="actions/runs",
        field="source_run_url",
    )
    if src_id != request.evidence.source_run_id:
        raise ValueError("evidence.source_run_id must match source_run_url")
    stg_id = _github_evidence_number(
        request.evidence.staging_run_url,
        repository=repo,
        resource="actions/runs",
        field="staging_run_url",
    )
    pr_num = _github_evidence_number(
        request.evidence.reviewed_change_url,
        repository=repo,
        resource="pull",
        field="reviewed_change_url",
    )
    pol = policy or _fetch_production_evidence_policy(request, fetch_json=fetch)
    src_run = fetch(f"/repos/{repo}/actions/runs/{src_id}")
    stg_run = fetch(f"/repos/{repo}/actions/runs/{stg_id}")
    pull = fetch(f"/repos/{repo}/pulls/{pr_num}")
    reviews = fetch(f"/repos/{repo}/pulls/{pr_num}/reviews")
    if not isinstance(reviews, Sequence) or isinstance(reviews, (str, bytes, Mapping)):
        raise ValueError(
            f"GitHub evidence response for /repos/{repo}/pulls/{pr_num}/reviews must be a list"
        )
    for run, label, url, exp in (
        (src_run, "source run", request.evidence.source_run_url, pol.source),
        (stg_run, "staging run", request.evidence.staging_run_url, pol.staging),
    ):
        _verify_run(
            run,
            label=label,
            repository=repo,
            url=url,
            sha=request.source_sha if exp.require_head_sha else None,
            event=exp.event,
            workflow_path=exp.workflow_path,
            display_title=exp.expected_display_title(request.version_ref),
        )
    _verify_reviewed_pull(
        pull,
        reviews=reviews,
        repository=repo,
        url=request.evidence.reviewed_change_url,
        sha=request.source_sha,
        base_ref=pol.review_base_ref,
    )


def _require_httpx() -> Any:
    from infra2_sdk.runtime._optional import require_httpx

    return require_httpx()


HttpGet = Callable[[str], tuple[int, str]]


@dataclass(frozen=True)
class _HealthCheckResult:
    attempts: int
    status_code: int
    body: str


def poll_until_healthy(
    url: str,
    *,
    http_get: HttpGet,
    expected_version: str = "",
    version_json_keys: tuple[str, ...] = ("git_sha", "version"),
    require_status: str | None = None,
    max_attempts: int = 24,
    max_version_mismatch_attempts: int | None = None,
    interval_seconds: float = 10.0,
    sleep: Callable[[float], None] = time.sleep,
) -> _HealthCheckResult:
    budget = max_version_mismatch_attempts or max_attempts
    streak, last_mismatch, last_status = 0, "", 0
    for attempt in range(1, max_attempts + 1):
        status_code, body = http_get(url)
        last_status = status_code
        if status_code != 200:
            _maybe_sleep(sleep, interval_seconds, attempt, max_attempts)
            continue
        parsed = _parse_json_object(body)
        if require_status is not None and (not parsed or parsed.get("status") != require_status):
            _maybe_sleep(sleep, interval_seconds, attempt, max_attempts)
            continue
        if expected_version:
            actual = _first_present(parsed, version_json_keys) if parsed else ""
            if not _version_prefix_matches(actual, expected_version):
                streak = streak + 1 if actual == last_mismatch else 1
                last_mismatch = actual
                if streak >= budget:
                    raise RuntimeError(
                        f"{url}: still reporting version {actual!r} (expected "
                        f"{expected_version!r} or a prefix match) after {streak} stable mismatches"
                    )
                _maybe_sleep(sleep, interval_seconds, attempt, max_attempts)
                continue
        return _HealthCheckResult(attempts=attempt, status_code=status_code, body=body)
    raise RuntimeError(
        f"{url}: did not become healthy after {max_attempts} attempts "
        f"(last status: HTTP {last_status})"
    )


def default_http_get(*, timeout: float = 10.0, follow_redirects: bool = True) -> HttpGet:
    httpx = _require_httpx()

    def http_get(url: str) -> tuple[int, str]:
        try:
            resp = httpx.get(url, timeout=timeout, follow_redirects=follow_redirects)
        except httpx.HTTPError as exc:
            return 0, str(exc)
        return resp.status_code, resp.text

    return http_get


def _maybe_sleep(
    sleep: Callable[[float], None], interval: float, attempt: int, max_attempts: int
) -> None:
    if attempt < max_attempts:
        sleep(interval)


def _parse_json_object(body: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _first_present(parsed: dict[str, Any], keys: tuple[str, ...]) -> str:
    return next((v for k in keys if isinstance(v := parsed.get(k), str) and v), "")


def _version_prefix_matches(actual: str, expected: str) -> bool:
    return bool(actual) and (actual.startswith(expected) or expected.startswith(actual))


_SAFE_REF_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._/-]{0,199}$")
_WHEEL_PIN_RE = re.compile(
    r"(?:infra2_sdk-(?P<whl>\d+\.\d+\.\d+)-py3-none-any\.whl"
    r"|infra2-sdk(?:\[[^\]]+\])?\s*@\s*[^#\n]*?@v(?P<tag>\d+\.\d+\.\d+))"
)
VERSION_KEYS = ("git_sha", "version")


def parse_wheel_version(text: str) -> str:
    found = {m.group("whl") or m.group("tag") for m in _WHEEL_PIN_RE.finditer(text)}
    if not found:
        raise ValueError("no pinned infra2-sdk wheel or @vX.Y.Z reference found")
    if len(found) > 1:
        raise ValueError(f"conflicting infra2-sdk pins found: {sorted(found)}")
    return next(iter(found))


class ReleaseIdentityError(RuntimeError):
    pass


def identity_from_body(body: str, *, url: str = "") -> str:
    where = url or "the health endpoint"
    try:
        payload = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise ReleaseIdentityError(f"{where} did not answer JSON: {body[:120]!r}") from exc
    if not isinstance(payload, dict):
        raise ReleaseIdentityError(f"{where} answered JSON that is not an object: {body[:120]!r}")
    value = next(
        (v for k in VERSION_KEYS if isinstance(v := payload.get(k), str) and v and v != "unknown"),
        None,
    )
    if value is None:
        raise ReleaseIdentityError(
            f"{where} does not report a release identity (body {body[:120]!r})"
        )
    if not _SAFE_REF_RE.match(value):
        raise ReleaseIdentityError(
            f"{where} reports {value!r}, which is not a usable release identifier"
        )
    return value


def read_deployed_release(url: str, http_get: HttpGet) -> str:
    status_code, body = http_get(url)
    if status_code != 200:
        raise ReleaseIdentityError(f"{url} answered HTTP {status_code}; its release is unknown")
    return identity_from_body(body, url=url)


def _main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True)
    p.add_argument("--expected-version", default="")
    p.add_argument("--require-status", default=None)
    p.add_argument("--max-attempts", type=int, default=24)
    p.add_argument("--interval-seconds", type=float, default=10.0)
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--no-follow-redirects", action="store_true")
    args = p.parse_args(list(argv) if argv is not None else None)
    getter = default_http_get(timeout=args.timeout, follow_redirects=not args.no_follow_redirects)
    try:
        res = poll_until_healthy(
            args.url,
            http_get=getter,
            expected_version=args.expected_version,
            require_status=args.require_status,
            max_attempts=args.max_attempts,
            interval_seconds=args.interval_seconds,
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"healthy after {res.attempts} attempt(s): HTTP {res.status_code}")
    return 0


__all__ = [
    "canonical_json_bytes",
    "DeployOperation",
    "DeployRequest",
    "DeployType",
    "PRODUCTION_DEPLOY_BUDGET_SECONDS",
    "PRODUCTION_REQUEST_MAX_AGE_SECONDS",
    "ReleaseIdentityError",
    "VERSION_KEYS",
    "default_http_get",
    "identity_from_body",
    "parse_sender_deadline",
    "parse_wheel_version",
    "poll_until_healthy",
    "production_request_accept_until",
    "read_deployed_release",
    "validate_evidence_urls",
    "verify_production_evidence",
]

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
