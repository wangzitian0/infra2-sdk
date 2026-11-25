"""Wheel installation smoke checks across core and all optional extras."""

from __future__ import annotations

import importlib
import sys

import infra2_sdk

EXPECTED_PUBLIC_MODULES = ("config", "telemetry", "service", "storage", "llm", "delivery")

EXTRA_IMPORTS: dict[str, tuple[str, ...]] = {
    "core": (),
    "telemetry": ("opentelemetry.trace", "opentelemetry.sdk.trace", "opentelemetry.metrics"),
    "http": ("httpx",),
    "fastapi": (
        "fastapi",
        "httpx",
        "opentelemetry.trace",
        "opentelemetry.sdk.trace",
        "opentelemetry.instrumentation.logging",
    ),
    "postgres": ("psycopg",),
    "s3": ("boto3", "botocore"),
    "llm": ("pydantic_ai", "openai", "httpx", "opentelemetry.trace"),
}
EXTRA_IMPORTS["all"] = tuple(
    sorted({mod for key, mods in EXTRA_IMPORTS.items() if key != "core" for mod in mods})
)


def main(extra: str) -> int:
    if extra not in EXTRA_IMPORTS:
        raise ValueError(f"unknown extra {extra!r}; expected one of {sorted(EXTRA_IMPORTS)}")
    assert tuple(infra2_sdk.__all__) == EXPECTED_PUBLIC_MODULES, infra2_sdk.__all__
    for name in EXPECTED_PUBLIC_MODULES:
        importlib.import_module(f"infra2_sdk.{name}")
    for mod_name in EXTRA_IMPORTS[extra]:
        importlib.import_module(mod_name)
    print(f"wheel_smoke ok for extra={extra} version={infra2_sdk.__version__}")
    return 0


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "core"
    raise SystemExit(main(target))
