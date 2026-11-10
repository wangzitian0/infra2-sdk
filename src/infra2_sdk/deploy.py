"""Versioned wire contract between an application and the infra2 deploy receiver."""

from __future__ import annotations

import binascii
import json
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import urlopen

from infra2_sdk._wire import (
    canonical_json_bytes,
    parse_contract_version,
    parse_iso_utc,
    parse_string,
    require_contract_version,
)
from infra2_sdk.transport import (
    _decode_github_contents,
    _github_api_url,
    _github_headers,
    _urlopen_json,
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


#: Maximum age in seconds of a production deploy request at the receiver (24 min, #1253).
#: The receiver uses it only for a request with no ``sender_deadline``.
PRODUCTION_REQUEST_MAX_AGE_SECONDS = 24 * 60
#: The time that the receiver keeps for the production deploy job before the sender
#: deadline (infra2#1232, infra2#1363). The longest of 25 production deploy jobs took 293 s.
PRODUCTION_DEPLOY_BUDGET_SECONDS = 300
#: A GitHub Actions job runs for 6 h at most, so no sender waits longer after its dispatch.
SENDER_MAX_WAIT_SECONDS = 6 * 60 * 60
#: The dispatch payload key that holds the time when the sender stops waiting (infra2#1363).
#: The dispatching sender workflow sets it. It is transport data, not a ``DeployRequest`` field.
SENDER_DEADLINE_KEY = "sender_deadline"


def parse_sender_deadline(payload: Mapping[str, Any]) -> datetime | None:
    """Return the ``sender_deadline`` of a dispatch payload in UTC, or None when it is absent.

    An old sender sends no deadline. A value that is present must be an ISO 8601 time
    with a time zone; any other value raises ``ValueError``.
    """
    value = payload.get(SENDER_DEADLINE_KEY)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{SENDER_DEADLINE_KEY} must be an ISO 8601 string")
    try:
        return parse_iso_utc(value)
    except ValueError:
        raise ValueError(
            f"{SENDER_DEADLINE_KEY} {value!r} is not an ISO 8601 time with a time zone"
        ) from None


def production_request_accept_until(
    created: datetime, sender_deadline: datetime | None
) -> datetime:
    """Return the last time at which the receiver accepts a production request.

    ``created`` is the creation time of the receiver run. The deploy must end while its
    sender still waits, because only the sender verifies the deploy (infra2#1232). With
    a ``sender_deadline``, the limit is that deadline minus
    ``PRODUCTION_DEPLOY_BUDGET_SECONDS``. A deadline later than
    ``SENDER_MAX_WAIT_SECONDS`` after ``created`` cannot be true and is cut to that
    time. With no deadline, the limit is ``PRODUCTION_REQUEST_MAX_AGE_SECONDS`` after
    ``created``.
    """
    if created.tzinfo is None:
        raise ValueError("the creation time must have a time zone")
    created = created.astimezone(UTC)
    if sender_deadline is None:
        return created + timedelta(seconds=PRODUCTION_REQUEST_MAX_AGE_SECONDS)
    if sender_deadline.tzinfo is None:
        raise ValueError("the sender deadline must have a time zone")
    latest = created + timedelta(seconds=SENDER_MAX_WAIT_SECONDS)
    deadline = min(sender_deadline.astimezone(UTC), latest)
    return deadline - timedelta(seconds=PRODUCTION_DEPLOY_BUDGET_SECONDS)


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
        evidence = raw.get("evidence")
        if not isinstance(evidence, Mapping):
            raise ValueError("evidence must be an object")
        contract_version = parse_contract_version(
            raw,
            CONTRACT_VERSION,
            description="contract_version",
        )
        return cls(
            contract_version=contract_version,
            request_id=parse_string(raw, "request_id"),
            operation=DeployOperation(parse_string(raw, "operation")),
            service=parse_string(raw, "service"),
            deploy_type=DeployType(parse_string(raw, "deploy_type")),
            version_ref=parse_string(raw, "version_ref"),
            source_repository=parse_string(raw, "source_repository"),
            source_sha=parse_string(raw, "source_sha").lower(),
            evidence=DeployEvidence.from_dict(evidence),
        )


def _validate_deploy_request(request: DeployRequest) -> None:
    require_contract_version(
        request.contract_version,
        CONTRACT_VERSION,
        description="contract_version",
    )
    if not _REQUEST_ID_RE.match(request.request_id):
        raise ValueError("request_id must be 8-128 URL-safe characters")
    if not _SERVICE_RE.match(request.service):
        raise ValueError("service must have the form project/service")
    if not request.version_ref.strip():
        raise ValueError("version_ref is required")
    if not _REPOSITORY_RE.match(request.source_repository):
        raise ValueError("source_repository must have the form owner/repository")
    if not _SHA40_RE.match(request.source_sha):
        raise ValueError("source_sha must be a lowercase 40-hex commit sha")
    if not request.evidence.source_run_url.startswith("https://github.com/"):
        raise ValueError("evidence.source_run_url must be a GitHub URL")
    if request.deploy_type == DeployType.PRODUCTION and not (
        request.evidence.staging_run_url and request.evidence.reviewed_change_url
    ):
        raise ValueError("production deploys require staging and reviewed-change evidence")
    if request.operation == DeployOperation.REMOVE and request.deploy_type not in {
        DeployType.PREVIEW_BRANCH,
        DeployType.PREVIEW_PR,
        DeployType.PREVIEW_COMMIT,
        DeployType.PREVIEW_TAG,
        DeployType.CANARY,
    }:
        raise ValueError("remove is limited to preview and canary targets")


# --- Production evidence policy (infra2#576 / infra2-sdk#8) -----------------------
#
# Each application repo is the sole authority on its own CI facts: which workflow
# builds its release image, which one runs its staging deploy, and the exact
# ``run-name`` title each produces. The app checks an instance of this contract into
# its OWN repo at PRODUCTION_EVIDENCE_POLICY_PATH; infra2's deploy receiver fetches
# that file (read-only GitHub API, pinned to the release's source_sha) and verifies
# the production evidence runs against the app's own declared expectations — no
# hardcoded per-app dict in infra2, so a CI-layout change and its contract update
# land in the same PR in the same repo.

_EVIDENCE_POLICY_CONTRACT_VERSION = 1
PRODUCTION_EVIDENCE_POLICY_PATH = "tools/production_evidence_policy.json"

_WORKFLOW_PATH_RE = re.compile(r"\A\.github/workflows/[A-Za-z0-9._-]+\.ya?ml\Z")
_TITLE_PLACEHOLDER = "{version_ref}"
_RUN_EVENTS = frozenset({"push", "workflow_dispatch"})


@dataclass(frozen=True)
class _RunEvidenceExpectation:
    """What one evidence run (release-image build, or staging deploy) must look like.

    ``display_title_template`` is a literal string supporting ``{version_ref}``
    substitution ONLY — not a callable, not a regex (infra2#572: apps must not be
    able to declare logic infra2 can't safely evaluate, only a greppable literal
    that a same-repo test can compare against the workflow's own ``run-name``).

    ``require_head_sha``: whether the run's ``head_sha`` must equal the release's
    ``source_sha``. True for a tag-push build (the run IS the tag commit). A
    staging deploy dispatched on the default branch runs at whatever that
    branch's tip is — equal to the tag commit only until the next merge lands —
    so an app whose staging dispatch targets a branch declares False and the
    version linkage is carried by ``{version_ref}`` in the display title instead
    (the deploy receiver separately pins version_ref -> source_sha at execution
    time). Declared per-run, never silently skipped by the verifier.
    """

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
        require_head_sha = raw.get("require_head_sha", True)
        if type(require_head_sha) is not bool:
            raise ValueError("require_head_sha must be a boolean")
        return cls(
            workflow_path=parse_string(raw, "workflow_path"),
            event=parse_string(raw, "event"),
            display_title_template=parse_string(raw, "display_title_template"),
            require_head_sha=require_head_sha,
        )


@dataclass(frozen=True)
class ProductionEvidencePolicy:
    """An app repo's own declaration of its production evidence expectations.

    ``service`` binds the file to the one deploy_v2 service it describes, so the
    receiver can reject a copy-pasted contract that names a different service.
    """

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
        contract_version = parse_contract_version(
            raw,
            _EVIDENCE_POLICY_CONTRACT_VERSION,
            description="evidence policy contract_version",
        )
        source = raw.get("source")
        staging = raw.get("staging")
        if not isinstance(source, Mapping):
            raise ValueError("source must be an object")
        if not isinstance(staging, Mapping):
            raise ValueError("staging must be an object")
        return cls(
            contract_version=contract_version,
            service=parse_string(raw, "service"),
            source=_RunEvidenceExpectation.from_dict(source),
            staging=_RunEvidenceExpectation.from_dict(staging),
            review_base_ref=parse_string(raw, "review_base_ref"),
        )


def _default_github_json_fetcher(
    *,
    token: str = "",
    token_env: str = "GITHUB_TOKEN",
    user_agent: str = "infra2-sdk",
    timeout: float = 10.0,
) -> Callable[[str], Any]:
    """Return a GitHub API JSON fetcher using the standard library."""
    headers = _github_headers(token=token or os.getenv(token_env, ""), user_agent=user_agent)

    def fetch(path: str) -> Any:
        try:
            return _urlopen_json(
                _github_api_url(path), headers=headers, timeout=timeout, opener=urlopen
            )
        except HTTPError as exc:
            raise ValueError(
                f"GitHub evidence request failed for {path}: HTTP {exc.code}"
            ) from None
        except (URLError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"GitHub evidence request failed for {path}: {type(exc).__name__}"
            ) from None

    return fetch


def _require_github_path(url: str, *, prefix: str, field: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or not parsed.path.startswith(prefix)
    ):
        raise ValueError(f"evidence.{field} must point to {prefix} on github.com")


def _github_evidence_number(
    url: str,
    *,
    repository: str,
    resource: str,
    field: str,
) -> str:
    parsed = urlparse(url)
    prefix = f"/{repository}/{resource}/"
    number = parsed.path.removeprefix(prefix)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or not parsed.path.startswith(prefix)
        or not number.isdigit()
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"evidence.{field} must be a canonical github.com {resource} URL")
    return number


def validate_evidence_urls(
    request: DeployRequest, *, expected_repository: str | None = None
) -> None:
    """Check that evidence URLs are canonical github.com URLs of one repository.

    Use ``expected_repository`` to pin the repository from a trusted source.
    Without it, the check uses ``request.source_repository``.
    """
    if expected_repository is not None and expected_repository != request.source_repository:
        raise ValueError("source_repository does not match the expected repository")
    repository_path = f"/{expected_repository or request.source_repository}/"
    _require_github_path(
        request.evidence.source_run_url,
        prefix=f"{repository_path}actions/runs/",
        field="source_run_url",
    )
    if request.deploy_type == DeployType.PRODUCTION:
        _require_github_path(
            request.evidence.staging_run_url,
            prefix=f"{repository_path}actions/runs/",
            field="staging_run_url",
        )
        _require_github_path(
            request.evidence.reviewed_change_url,
            prefix=f"{repository_path}pull/",
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
    remote_repository = run.get("repository")
    if (
        not isinstance(remote_repository, Mapping)
        or remote_repository.get("full_name") != repository
    ):
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
    remote_repository = base.get("repo") if isinstance(base, Mapping) else None
    if (
        not isinstance(remote_repository, Mapping)
        or remote_repository.get("full_name") != repository
    ):
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
        pull_author = user.get("login") if isinstance(user, Mapping) else None
        latest_states = {
            r["user"]["login"]: r["state"]
            for r in reviews
            if isinstance(r, Mapping)
            and isinstance(r.get("user"), Mapping)
            and isinstance(r["user"].get("login"), str)
            and r["user"]["login"]
            and isinstance(r.get("state"), str)
        }
        if pull_author:
            latest_states.pop(pull_author, None)
        if any(s == "CHANGES_REQUESTED" for s in latest_states.values()):
            raise ValueError("reviewed pull request has pending CHANGES_REQUESTED")


def _parse_evidence_policy_payload(
    where: str,
    payload: Any,
    *,
    service: str | None = None,
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
        policy = ProductionEvidencePolicy.from_dict(raw)
    except ValueError as exc:
        raise ValueError(f"{where} is not a valid evidence contract: {exc}") from exc
    if service is not None and policy.service != service:
        raise ValueError(f"{where} declares service {policy.service!r}, not {service!r}")
    return policy


def _fetch_production_evidence_policy(
    request: DeployRequest,
    *,
    fetch_json: Callable[[str], Any] | None = None,
) -> ProductionEvidencePolicy:
    """Fetch and validate the production evidence policy for a request."""
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
            f"fetching {where} failed ({exc}). Production releases require the "
            "app repo to declare its own contract file (infra2#576); without one "
            "the app is staging-only."
        ) from exc
    return _parse_evidence_policy_payload(where, payload, service=request.service)


def verify_production_evidence(
    request: DeployRequest,
    policy: ProductionEvidencePolicy | None = None,
    *,
    fetch_json: Callable[[str], Any] | None = None,
) -> None:
    """Verify production evidence runs and pull requests against GitHub API."""
    fetch = fetch_json or _default_github_json_fetcher()
    _validate_evidence_urls(request)
    source_run_id = _github_evidence_number(
        request.evidence.source_run_url,
        repository=request.source_repository,
        resource="actions/runs",
        field="source_run_url",
    )
    if source_run_id != request.evidence.source_run_id:
        raise ValueError("evidence.source_run_id must match source_run_url")
    staging_run_id = _github_evidence_number(
        request.evidence.staging_run_url,
        repository=request.source_repository,
        resource="actions/runs",
        field="staging_run_url",
    )
    pull_number = _github_evidence_number(
        request.evidence.reviewed_change_url,
        repository=request.source_repository,
        resource="pull",
        field="reviewed_change_url",
    )
    effective_policy = policy or _fetch_production_evidence_policy(request, fetch_json=fetch)

    source_run = fetch(f"/repos/{request.source_repository}/actions/runs/{source_run_id}")
    staging_run = fetch(f"/repos/{request.source_repository}/actions/runs/{staging_run_id}")
    reviewed_pull = fetch(f"/repos/{request.source_repository}/pulls/{pull_number}")
    reviews = fetch(f"/repos/{request.source_repository}/pulls/{pull_number}/reviews")
    if not isinstance(reviews, Sequence) or isinstance(reviews, (str, bytes, Mapping)):
        raise ValueError(
            f"GitHub evidence response for "
            f"/repos/{request.source_repository}/pulls/{pull_number}/reviews must be a list"
        )
    for run, label, url, expectation in (
        (source_run, "source run", request.evidence.source_run_url, effective_policy.source),
        (staging_run, "staging run", request.evidence.staging_run_url, effective_policy.staging),
    ):
        _verify_run(
            run,
            label=label,
            repository=request.source_repository,
            url=url,
            sha=request.source_sha if expectation.require_head_sha else None,
            event=expectation.event,
            workflow_path=expectation.workflow_path,
            display_title=expectation.expected_display_title(request.version_ref),
        )
    _verify_reviewed_pull(
        reviewed_pull,
        reviews=reviews,
        repository=request.source_repository,
        url=request.evidence.reviewed_change_url,
        sha=request.source_sha,
        base_ref=effective_policy.review_base_ref,
    )


def _require_httpx() -> Any:
    from infra2_sdk.runtime._optional import require_httpx

    return require_httpx()


# --- Deploy Health Polling ---------------------------------------------------

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
    """Poll ``url`` until healthy, or raise ``RuntimeError`` once exhausted."""
    mismatch_budget = max_version_mismatch_attempts or max_attempts
    mismatch_streak = 0
    last_mismatch = ""
    last_status = 0

    for attempt in range(1, max_attempts + 1):
        status_code, body = http_get(url)
        last_status = status_code

        if status_code != 200:
            _maybe_sleep(sleep, interval_seconds, attempt, max_attempts)
            continue

        parsed = _parse_json_object(body)
        if require_status is not None and (
            parsed is None or parsed.get("status") != require_status
        ):
            _maybe_sleep(sleep, interval_seconds, attempt, max_attempts)
            continue

        if expected_version:
            actual = _first_present(parsed, version_json_keys) if parsed else ""
            if not _version_prefix_matches(actual, expected_version):
                if actual == last_mismatch:
                    mismatch_streak += 1
                else:
                    last_mismatch = actual
                    mismatch_streak = 1
                if mismatch_streak >= mismatch_budget:
                    raise RuntimeError(
                        f"{url}: still reporting version {actual!r} (expected "
                        f"{expected_version!r} or a prefix match) after "
                        f"{mismatch_streak} stable mismatches"
                    )
                _maybe_sleep(sleep, interval_seconds, attempt, max_attempts)
                continue

        return _HealthCheckResult(attempts=attempt, status_code=status_code, body=body)

    raise RuntimeError(
        f"{url}: did not become healthy after {max_attempts} attempts "
        f"(last status: HTTP {last_status})"
    )


def default_http_get(*, timeout: float = 10.0) -> HttpGet:
    """httpx-backed ``http_get``: returns ``(status_code, body_text)``, or
    ``(0, error-text)`` on any connection-level failure."""
    httpx = _require_httpx()

    def http_get(url: str) -> tuple[int, str]:
        try:
            response = httpx.get(url, timeout=timeout, follow_redirects=True)
        except httpx.HTTPError as exc:
            return 0, str(exc)
        return response.status_code, response.text

    return http_get


def _maybe_sleep(
    sleep: Callable[[float], None],
    interval_seconds: float,
    attempt: int,
    max_attempts: int,
) -> None:
    if attempt < max_attempts:
        sleep(interval_seconds)


def _parse_json_object(body: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _first_present(parsed: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = parsed.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _version_prefix_matches(actual: str, expected: str) -> bool:
    if not actual:
        return False
    return actual.startswith(expected) or expected.startswith(actual)


_SAFE_REF_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._/-]{0,199}$")
VERSION_KEYS = ("git_sha", "version")


class ReleaseIdentityError(RuntimeError):
    """The environment's release identity could not be established."""


def identity_from_body(body: str, *, url: str = "") -> str:
    """Extract the release identity from a health payload or raise ReleaseIdentityError."""
    where = url or "the health endpoint"
    try:
        payload = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise ReleaseIdentityError(f"{where} did not answer JSON: {body[:120]!r}") from exc
    if not isinstance(payload, dict):
        raise ReleaseIdentityError(f"{where} answered JSON that is not an object: {body[:120]!r}")

    for key in VERSION_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value and value != "unknown":
            break
    else:
        raise ReleaseIdentityError(
            f"{where} does not report a release identity (body {body[:120]!r}); nothing about "
            f"the deployed release can be judged until the deployer threads GIT_COMMIT_SHA through"
        )

    if not _SAFE_REF_RE.match(value):
        raise ReleaseIdentityError(
            f"{where} reports {value!r}, which is not a usable release identifier — a leading "
            f"'-' would be read by git as an option, and whitespace makes the failure "
            f"non-deterministic. This value is not used"
        )
    return value


def read_deployed_release(url: str, http_get: HttpGet) -> str:
    """Fetch ``url`` and return the release identity it reports, or raise ReleaseIdentityError."""
    status_code, body = http_get(url)
    if status_code != 200:
        raise ReleaseIdentityError(f"{url} answered HTTP {status_code}; its release is unknown")
    return identity_from_body(body, url=url)


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
    "poll_until_healthy",
    "production_request_accept_until",
    "read_deployed_release",
    "validate_evidence_urls",
    "verify_production_evidence",
]
