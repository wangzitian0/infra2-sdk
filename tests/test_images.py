from __future__ import annotations

import importlib
import warnings
from importlib.resources import files

import pytest

# The module warns on import; the import-time warning itself is asserted in a test below.
with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    images = importlib.import_module("infra2_sdk.images")
get_platform_image = images.get_platform_image
get_platform_image_spec = images.get_platform_image_spec
load_platform_images = images.load_platform_images

pytestmark = pytest.mark.filterwarnings("ignore:infra2_sdk.images is deprecated:DeprecationWarning")


def test_load_platform_images() -> None:
    catalog = load_platform_images()
    assert set(catalog) == {"postgres", "redis", "vault_agent"}


def test_catalog_no_longer_advertises_minio_or_mc() -> None:
    # infra2 runs RustFS now: neither the entries nor any mention may remain in the data file.
    assert not {"minio", "mc"} & set(load_platform_images())
    packaged = files("infra2_sdk").joinpath("data/platform_images.yaml").read_text("utf-8")
    entries = [line for line in packaged.splitlines() if not line.lstrip().startswith("#")]
    assert "minio" not in "\n".join(entries).lower()
    assert "mc-mirror" not in "\n".join(entries)


def test_get_platform_image() -> None:
    postgres = get_platform_image("postgres")
    assert postgres == "postgres:16-alpine"

    vault = get_platform_image("vault_agent")
    assert vault == "hashicorp/vault:1.15"

    with pytest.raises(KeyError, match="unknown platform image"):
        get_platform_image("nonexistent_service")
    with pytest.raises(KeyError, match="unknown platform image"):
        get_platform_image("minio")


def test_get_platform_image_spec() -> None:
    spec = get_platform_image_spec("redis")
    assert spec["image"] == "redis:7-alpine"
    assert spec["recommended_mem_limit"] == "256m"
    with pytest.raises(KeyError, match="unknown platform image"):
        get_platform_image_spec("mc")


def test_importing_the_module_warns_that_it_is_removed_in_3_0_0() -> None:
    with pytest.warns(DeprecationWarning, match=r"deprecated since 2\.4\.0.*removed in 3\.0\.0"):
        importlib.reload(images)


@pytest.mark.parametrize(
    "call",
    [
        lambda: load_platform_images(),
        lambda: get_platform_image("postgres"),
        lambda: get_platform_image_spec("postgres"),
    ],
)
def test_every_entry_point_warns_once_and_attributes_it_to_the_caller(call) -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        call()
    deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(deprecations) == 1
    assert deprecations[0].filename == __file__


def test_an_explicit_catalog_still_warns_but_is_used_as_given() -> None:
    catalog = {"custom": {"image": "example/custom:1"}}
    with pytest.warns(DeprecationWarning):
        assert get_platform_image("custom", catalog=catalog) == "example/custom:1"


def test_a_catalog_file_without_an_images_mapping_is_rejected(tmp_path) -> None:
    bad = tmp_path / "platform_images.yaml"
    bad.write_text("schema_version: 1\nimages: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="'images' must be a mapping"):
        load_platform_images(bad)
    good = tmp_path / "good.yaml"
    good.write_text("images:\n  custom:\n    image: example/custom:1\n", encoding="utf-8")
    assert load_platform_images(good) == {"custom": {"image": "example/custom:1"}}
