from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("setuptools")
pytest.importorskip("setuptools_scm")

import build_backend


def test_build_wheel_pins_version_before_rewriting_readme(
    monkeypatch, tmp_path: Path
) -> None:
    readme_path = tmp_path / "README.md"
    original_readme = "[Doc](README.md)\n"
    readme_path.write_text(original_readme, encoding="utf-8")

    observed: dict[str, str | None] = {}

    def fake_get_version(*, root: Path) -> str:
        observed["version_readme"] = readme_path.read_text(encoding="utf-8")
        observed["version_root"] = str(root)
        return "0.1.2"

    def fake_build_wheel(
        wheel_directory: str, config_settings=None, metadata_directory=None
    ) -> str:
        observed["pretend_version"] = os.environ.get("SETUPTOOLS_SCM_PRETEND_VERSION")
        observed["build_readme"] = readme_path.read_text(encoding="utf-8")
        return "emx_regex_cgen-0.1.2-py3-none-any.whl"

    monkeypatch.setattr(build_backend, "README_PATH", readme_path)
    monkeypatch.setattr(build_backend, "get_version", fake_get_version)
    monkeypatch.setattr(build_backend._build_meta, "build_wheel", fake_build_wheel)

    wheel_name = build_backend.build_wheel(str(tmp_path))

    assert wheel_name == "emx_regex_cgen-0.1.2-py3-none-any.whl"
    assert observed["version_readme"] == original_readme
    assert observed["pretend_version"] == "0.1.2"
    assert (
        observed["build_readme"]
        == "[Doc](https://github.com/emmtrix/emx-regex-cgen/blob/v0.1.2/README.md)\n"
    )
    assert readme_path.read_text(encoding="utf-8") == original_readme
    assert "SETUPTOOLS_SCM_PRETEND_VERSION" not in os.environ
