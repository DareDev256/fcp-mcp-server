"""What ships in the wheel, checked against what server.py imports.

Four releases (0.19.2 through 0.21.0) shipped a package that raised
ModuleNotFoundError at import: `pyproject.toml` declared
`include = ["fcpxml*"]`, so setuptools silently omitted `tools/`, which
server.py imports at module scope. Every install from PyPI died before it
could answer initialize, and the MCP client reported only "Connection
closed". None of it was visible from a git checkout, where Python finds
tools/ in the working directory — which is why the whole suite stayed green
across all four.

Reported by a user on 2026-09-03, not caught here. This file is the check
that would have.
"""

import ast
import fnmatch
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _include_patterns() -> list[str]:
    text = (ROOT / "pyproject.toml").read_text()
    block = re.search(
        r"\[tool\.setuptools\.packages\.find\](.*?)(?=\n\[|\Z)", text, re.S
    )
    assert block, "packages.find section is gone — packaging moved, update this test"
    include = re.search(r"^include = (\[.*?\])", block.group(1), re.M | re.S)
    assert include, "no include list under packages.find"
    return ast.literal_eval(include.group(1))


def _py_modules() -> list[str]:
    text = (ROOT / "pyproject.toml").read_text()
    found = re.search(r"^py-modules = (\[.*?\])", text, re.M | re.S)
    return ast.literal_eval(found.group(1)) if found else []


def _top_level_imports_of(module: Path) -> set[str]:
    tree = ast.parse(module.read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def _local_packages() -> set[str]:
    return {
        p.name for p in ROOT.iterdir()
        if p.is_dir() and (p / "__init__.py").is_file() and not p.name.startswith(".")
    }


def test_every_local_package_server_imports_is_packaged():
    """The exact failure: a first-party package imported but never shipped."""
    patterns = _include_patterns()
    needed = _top_level_imports_of(ROOT / "server.py") & _local_packages()
    assert needed, "server.py imports no local package — the detection broke"

    unpackaged = [
        name for name in sorted(needed)
        if not any(fnmatch.fnmatch(name, pattern) for pattern in patterns)
    ]
    assert not unpackaged, (
        f"server.py imports {unpackaged} but pyproject include={patterns} does not "
        f"match them. A wheel built from this raises ModuleNotFoundError at import, "
        f"and the MCP client shows only 'Connection closed'."
    )


def test_tools_is_packaged_by_name():
    """Named explicitly, not just derived — the derivation could break too."""
    assert any(
        fnmatch.fnmatch("tools", pattern) for pattern in _include_patterns()
    ), "tools/ must be in the wheel: server.py imports it at module scope"


def test_the_packages_imported_by_the_tools_package_are_packaged_too():
    """tools/ modules import fcpxml; a wheel missing that fails the same way."""
    patterns = _include_patterns() + _py_modules()
    local = _local_packages() | {p.stem for p in ROOT.glob("*.py")}
    for module in sorted((ROOT / "tools").glob("*.py")):
        for name in sorted(_top_level_imports_of(module) & local):
            assert any(fnmatch.fnmatch(name, pattern) for pattern in patterns), (
                f"tools/{module.name} imports {name!r}, which is not packaged"
            )


def test_the_published_author_address_is_not_the_one_that_bounced():
    """dare@jamesdare.com does not exist; it was on every release to 0.21.0.

    A user hit the bounce and had to dig an address out of the commit history
    to report the packaging bug above.
    """
    text = (ROOT / "pyproject.toml").read_text()
    assert "dare@jamesdare.com" not in text
    assert re.search(r'email = "[^"]+@[^"]+"', text), "no author email at all"
