#!/usr/bin/env python3
"""The gate: the ohmyboring core package and its three integration siblings import from the
repo root, and each integration pyproject names itself ohmyboring-<fw> and depends on ohmyboring.

Run: python3 scripts/test_package_skeleton.py   (no pytest dependency)

Why this exists. Slice 1 of the ohmyboring restructuring stands up real packages before any
file moves: door image and hermes container must be able to `import ohmyboring` from the day
the skeleton lands. A green that is not asserted degrades the moment a slice moves code into
a folder the packaging does not see.
"""

import importlib
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CORE_MODULES = [
    "ohmyboring",
    "ohmyboring.adapters",
    "ohmyboring.eval",
    "ohmyboring.entrypoints",
    "ohmyboring.entrypoints.cli",
    "ohmyboring.entrypoints.http",
    "ohmyboring.entrypoints.slack",
    "ohmyboring.entrypoints.mcp",
    "ohmyboring.entrypoints.hermes",
    "ohmyboring.entrypoints.hooks",
]

FRAMEWORKS = ["langchain", "langgraph", "deepagents"]


def _import_from_cwd(module: str) -> None:
    """Import with the repo root on sys.path — the contract is "works from the repo root",
    not "works when installed" (installation lands with the delivery slice's image build)."""
    saved = list(sys.path)
    sys.path.insert(0, str(ROOT))
    try:
        importlib.import_module(module)
    finally:
        sys.path[:] = saved


def test_core_package_imports_from_repo_root():
    for module in CORE_MODULES:
        _import_from_cwd(module)


def test_integration_packages_import_from_repo_root():
    for fw in FRAMEWORKS:
        src = ROOT / "integrations" / fw / "src"
        assert src.is_dir(), f"integrations/{fw}/src is missing"
        sys.path.insert(0, str(src))
        try:
            importlib.import_module(f"ohmyboring_{fw}")
        finally:
            sys.path.remove(str(src))


def test_integration_pyprojects_name_and_dependency():
    for fw in FRAMEWORKS:
        pyproject = ROOT / "integrations" / fw / "pyproject.toml"
        assert pyproject.is_file(), f"{pyproject.relative_to(ROOT)} is missing"
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        project = data["project"]
        assert project["name"] == f"ohmyboring-{fw}", (
            f"{pyproject.relative_to(ROOT)}: project.name is {project['name']!r}, "
            f"expected 'ohmyboring-{fw}' (sibling package, published as ohmyboring-<fw>)"
        )
        assert "ohmyboring" in project["dependencies"], (
            f"{pyproject.relative_to(ROOT)}: integrations must depend on the core ohmyboring "
            f"package — dependencies are {project['dependencies']!r}"
        )


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok - package skeleton: core + 3 integrations import, pyprojects name/depend right")
