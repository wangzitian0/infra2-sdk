"""Versioned wire contract between an application and the infra2 deploy receiver."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import time
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, fields
from enum import StrEnum
from io import BytesIO
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from infra2_sdk._wire import (
    parse_contract_version,
    parse_string,
    require_contract_version,
    require_exact_fields,
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


class DeployState(StrEnum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"


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
        validate_deploy_request(self)

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


def validate_deploy_request(request: DeployRequest) -> None:
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


def validate_wire_shape(raw: Mapping[str, Any]) -> None:
    """Fail closed unless ``raw``'s keys are EXACTLY the DeployRequest v1 wire shape.

    ``DeployRequest.from_dict`` is deliberately lenient about extra/missing keys —
    it only reads what it needs, so a minor sdk field addition doesn't break an
    older sender. This is the opposite, stricter check for a sender that wants a
    hard guarantee it is emitting the current wire shape exactly: no field silently
    dropped, no unexpected extra field silently accepted. Expected field sets are
    derived from the dataclasses themselves (``dataclasses.fields``), not
    hand-copied, so they can never drift from the real contract the way a
    hardcoded ``frozenset`` in a caller repo can.

    Call this BEFORE ``DeployRequest.from_dict(raw)`` (so a structurally wrong
    payload fails with a shape error, not a confusing missing-field error), and
    call it again on ``request.to_dict()`` after round-tripping if the caller
    wants to prove serialization is lossless too.
    """
    expected = {f.name for f in fields(DeployRequest)}
    require_exact_fields(
        raw,
        expected,
        description="deploy request",
        contract_message="request fields must exactly match DeployRequest v1",
    )
    evidence = raw.get("evidence")
    expected_evidence = {f.name for f in fields(DeployEvidence)}
    require_exact_fields(
        evidence,  # type: ignore[arg-type]
        expected_evidence,
        description="evidence",
        contract_message="evidence fields must exactly match DeployEvidence v1",
    )


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

EVIDENCE_POLICY_CONTRACT_VERSION = 1
PRODUCTION_EVIDENCE_POLICY_PATH = "tools/production_evidence_policy.json"

_WORKFLOW_PATH_RE = re.compile(r"\A\.github/workflows/[A-Za-z0-9._-]+\.ya?ml\Z")
_TITLE_PLACEHOLDER = "{version_ref}"
_RUN_EVENTS = frozenset({"push", "workflow_dispatch"})


@dataclass(frozen=True)
class RunEvidenceExpectation:
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
    def from_dict(cls, raw: Mapping[str, Any]) -> RunEvidenceExpectation:
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
    source: RunEvidenceExpectation
    staging: RunEvidenceExpectation
    review_base_ref: str
    contract_version: int = EVIDENCE_POLICY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        require_contract_version(
            self.contract_version,
            EVIDENCE_POLICY_CONTRACT_VERSION,
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
            EVIDENCE_POLICY_CONTRACT_VERSION,
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
            source=RunEvidenceExpectation.from_dict(source),
            staging=RunEvidenceExpectation.from_dict(staging),
            review_base_ref=parse_string(raw, "review_base_ref"),
        )


def canonical_json(request: DeployRequest) -> str:
    """Return stable wire JSON for a deployment request."""
    return (
        json.dumps(
            request.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    )


def default_github_json_fetcher(
    *,
    token: str = "",
    token_env: str = "GITHUB_TOKEN",
    user_agent: str = "infra2-sdk",
    timeout: float = 10.0,
) -> Callable[[str], Any]:
    """Return a GitHub API JSON fetcher using the standard library."""
    auth_token = token or os.getenv(token_env, "")

    def fetch(path: str) -> Any:
        url = path if path.startswith("https://") else f"https://api.github.com{path}"
        req = Request(url)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", user_agent)
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if auth_token:
            req.add_header("Authorization", f"Bearer {auth_token}")
        try:
            with urlopen(req, timeout=timeout) as response:
                content = response.read()
                return json.loads(content)
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

        latest_states: dict[str, str] = {}
        for r in reviews:
            if not isinstance(r, Mapping):
                continue
            r_user = r.get("user")
            reviewer = r_user.get("login") if isinstance(r_user, Mapping) else None
            state = r.get("state")
            if reviewer and isinstance(state, str):
                latest_states[reviewer] = state

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
    if not isinstance(payload, Mapping):
        raise ValueError(f"{where} must contain a JSON object")
    content = payload.get("content")
    if not isinstance(content, str):
        raise ValueError(f"{where} did not return file content")
    try:
        raw = json.loads(base64.b64decode(content, validate=False))
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


def fetch_production_evidence_policy(
    request: DeployRequest,
    *,
    fetch_json: Callable[[str], Any] | None = None,
) -> ProductionEvidencePolicy:
    """Fetch and validate the production evidence policy for a request."""
    where = f"{request.source_repository}:{PRODUCTION_EVIDENCE_POLICY_PATH}@{request.source_sha}"
    fetch = fetch_json or default_github_json_fetcher()
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
    fetch = fetch_json or default_github_json_fetcher()
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
    effective_policy = policy or fetch_production_evidence_policy(request, fetch_json=fetch)

    source_run = fetch(f"/repos/{request.source_repository}/actions/runs/{source_run_id}")
    staging_run = fetch(f"/repos/{request.source_repository}/actions/runs/{staging_run_id}")
    reviewed_pull = fetch(f"/repos/{request.source_repository}/pulls/{pull_number}")
    reviews = fetch(f"/repos/{request.source_repository}/pulls/{pull_number}/reviews")
    if not isinstance(reviews, Sequence) or isinstance(reviews, (str, bytes, Mapping)):
        raise ValueError(
            f"GitHub evidence response for "
            f"/repos/{request.source_repository}/pulls/{pull_number}/reviews must be a list"
        )
    _verify_run(
        source_run,
        label="source run",
        repository=request.source_repository,
        url=request.evidence.source_run_url,
        sha=request.source_sha if effective_policy.source.require_head_sha else None,
        event=effective_policy.source.event,
        workflow_path=effective_policy.source.workflow_path,
        display_title=effective_policy.source.expected_display_title(request.version_ref),
    )
    _verify_run(
        staging_run,
        label="staging run",
        repository=request.source_repository,
        url=request.evidence.staging_run_url,
        sha=request.source_sha if effective_policy.staging.require_head_sha else None,
        event=effective_policy.staging.event,
        workflow_path=effective_policy.staging.workflow_path,
        display_title=effective_policy.staging.expected_display_title(request.version_ref),
    )
    _verify_reviewed_pull(
        reviewed_pull,
        reviews=reviews,
        repository=request.source_repository,
        url=request.evidence.reviewed_change_url,
        sha=request.source_sha,
        base_ref=effective_policy.review_base_ref,
    )


# --- Deploy Dispatch ---------------------------------------------------------

INFRA_REPOSITORY = "wangzitian0/infra2"
RECEIVER_WORKFLOW_FILE = "app-deploy-request.yml"
RECEIVER_EVENT_TYPE = "app-deploy-request"
_RUNS_PATH = (
    f"/repos/{INFRA_REPOSITORY}/actions/workflows/{RECEIVER_WORKFLOW_FILE}/runs"
    "?event=repository_dispatch&per_page=30"
)

Api = Callable[[str, str, object], object]
LogFetcher = Callable[[int], bytes]


@dataclass(frozen=True)
class ReceiverRun:
    run_id: int
    url: str


def dispatch_and_wait(
    request: DeployRequest,
    *,
    api: Api,
    fetch_logs: LogFetcher,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval: float = 5.0,
    max_attempts: int = 300,
) -> ReceiverRun:
    """Dispatch ``request`` to infra2 and return the correlated, verified receiver run."""
    canonical = request.to_dict()
    baseline = _workflow_runs(api("GET", _RUNS_PATH, None))
    watermark = max((_run_id(run) for run in baseline), default=0)

    api(
        "POST",
        f"/repos/{INFRA_REPOSITORY}/dispatches",
        {"event_type": RECEIVER_EVENT_TYPE, "client_payload": canonical},
    )

    seen: list[str] = []
    for attempt in range(max_attempts):
        runs = _workflow_runs(api("GET", _RUNS_PATH, None))
        fresh = [run for run in runs if _run_id(run) > watermark]
        seen = [f"{_run_id(run)} {run.get('display_title') or '(untitled)'}" for run in fresh]
        candidates = [
            run for run in fresh if request.request_id in str(run.get("display_title", ""))
        ]
        if len(candidates) > 1:
            ids = sorted(_run_id(run) for run in candidates)
            raise RuntimeError(
                f"receiver run correlation is ambiguous for request_id "
                f"{request.request_id!r} after watermark {watermark}: {ids}"
            )
        if not candidates:
            if attempt + 1 < max_attempts:
                sleep(poll_interval)
            continue

        run = candidates[0]
        if run.get("status") != "completed":
            if attempt + 1 < max_attempts:
                sleep(poll_interval)
            continue
        run_id = _run_id(run)
        if run.get("conclusion") != "success":
            raise RuntimeError(f"infra2 receiver run {run_id} concluded {run.get('conclusion')!r}")
        request_id = request.request_id.encode("utf-8")
        if request_id not in fetch_logs(run_id):
            raise RuntimeError(
                f"infra2 receiver run {run_id} logs do not contain request_id "
                f"{request.request_id!r}"
            )
        url = run.get("html_url")
        if not isinstance(url, str) or not url.startswith(
            f"https://github.com/{INFRA_REPOSITORY}/actions/runs/"
        ):
            raise RuntimeError(f"infra2 receiver run {run_id} has no canonical URL")
        return ReceiverRun(run_id=run_id, url=url)

    raise RuntimeError(
        f"timed out waiting for an infra2 receiver run naming request_id "
        f"{request.request_id!r} after watermark {watermark}; runs seen: {seen or 'none'}"
    )


def github_api_client(
    *,
    token: str,
    user_agent: str,
    timeout: float = 30.0,
    transport: Any = None,
) -> tuple[Api, LogFetcher]:
    """Build the default httpx-backed (``api``, ``fetch_logs``) pair for ``dispatch_and_wait``."""
    httpx = _require_httpx()
    client = httpx.Client(
        base_url="https://api.github.com",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": user_agent,
            "X-GitHub-Api-Version": "2022-11-28",
        },
        follow_redirects=True,
        timeout=timeout,
        transport=transport,
    )

    def api(method: str, path: str, body: object) -> object:
        response = client.request(method, path, json=body if method == "POST" else None)
        if response.status_code >= 400:
            endpoint = path.split("?", 1)[0]
            code = response.status_code
            raise RuntimeError(f"GitHub API {method} {endpoint} failed with HTTP {code}")
        if method == "POST":
            if response.status_code != 204:
                raise RuntimeError(f"GitHub dispatch expected HTTP 204, got {response.status_code}")
            return None
        try:
            return response.json()
        except ValueError:
            raise RuntimeError("GitHub API response was not valid JSON") from None

    def fetch_logs(run_id: int) -> bytes:
        response = client.get(f"/repos/{INFRA_REPOSITORY}/actions/runs/{run_id}/logs")
        if response.status_code >= 400:
            raise RuntimeError(
                f"GitHub receiver logs request failed with HTTP {response.status_code}"
            )
        try:
            with zipfile.ZipFile(BytesIO(response.content)) as archive:
                return b"\n".join(archive.read(name) for name in archive.namelist())
        except zipfile.BadZipFile:
            raise RuntimeError("GitHub receiver logs response was not a zip archive") from None

    api.close = client.close  # type: ignore[attr-defined]
    api.client = client  # type: ignore[attr-defined]
    fetch_logs.close = client.close  # type: ignore[attr-defined]
    fetch_logs.client = client  # type: ignore[attr-defined]
    return api, fetch_logs


def _require_httpx() -> Any:
    from infra2_sdk.runtime._optional import require_httpx

    return require_httpx()


def _workflow_runs(payload: object) -> list[Mapping[str, object]]:
    if not isinstance(payload, Mapping):
        raise RuntimeError("GitHub workflow-runs response must be an object")
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list) or not all(isinstance(run, Mapping) for run in runs):
        raise RuntimeError("GitHub workflow-runs response must contain a run list")
    return runs


def _run_id(run: Mapping[str, object]) -> int:
    run_id = run.get("id")
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
        raise RuntimeError("GitHub workflow run id must be a positive integer")
    return run_id


# --- Deploy Health Polling ---------------------------------------------------

HttpGet = Callable[[str], tuple[int, str]]


@dataclass(frozen=True)
class HealthCheckResult:
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
) -> HealthCheckResult:
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

        return HealthCheckResult(attempts=attempt, status_code=status_code, body=body)

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


__all__ = [
    "CONTRACT_VERSION",
    "DeployEvidence",
    "DeployOperation",
    "DeployRequest",
    "DeployState",
    "DeployType",
    "EVIDENCE_POLICY_CONTRACT_VERSION",
    "HealthCheckResult",
    "PRODUCTION_EVIDENCE_POLICY_PATH",
    "ProductionEvidencePolicy",
    "ReceiverRun",
    "RunEvidenceExpectation",
    "canonical_json",
    "default_github_json_fetcher",
    "dispatch_and_wait",
    "fetch_production_evidence_policy",
    "poll_until_healthy",
    "validate_deploy_request",
    "validate_evidence_urls",
    "validate_wire_shape",
    "verify_production_evidence",
    "Api",
    "HttpGet",
    "INFRA_REPOSITORY",
    "LogFetcher",
    "RECEIVER_EVENT_TYPE",
    "RECEIVER_WORKFLOW_FILE",
    "default_http_get",
    "github_api_client",
]
