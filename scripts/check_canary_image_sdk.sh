#!/usr/bin/env bash
# Fail when the Canary Todo image does not run the SDK version of pyproject.toml (#247).
set -euo pipefail
image="$1"
expected="$(python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
installed="$(docker run --rm --entrypoint python "$image" -c 'import importlib.metadata as m; print(m.version("infra2-sdk"))')"
if [ "$installed" != "$expected" ]; then
  echo "::error::image $image runs infra2-sdk $installed, pyproject.toml is $expected"
  exit 1
fi
echo "image $image runs infra2-sdk $installed"
