#!/usr/bin/env bash
# Fail when the Canary Todo image does not run the SDK version of pyproject.toml (#247).
set -euo pipefail
image="$1"
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  runtime=docker
elif command -v podman >/dev/null 2>&1; then
  runtime=podman
else
  echo "neither a running docker daemon nor podman is available" >&2
  exit 1
fi
expected="$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
installed="$("$runtime" run --rm --entrypoint python "$image" -c 'import importlib.metadata as m; print(m.version("infra2-sdk"))')"
if [ "$installed" != "$expected" ]; then
  echo "::error::image $image runs infra2-sdk $installed, pyproject.toml is $expected"
  exit 1
fi
echo "image $image runs infra2-sdk $installed"
