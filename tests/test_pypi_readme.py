"""The README PyPI renders must show its images (scripts/pypi_readme.py).

Every image on pypi.org/project/fcp-mcp-server was broken through 0.30.0:
the README used repository-relative paths, which GitHub resolves and PyPI
cannot. The publish workflow now rewrites them to URLs pinned to the
release tag before building, and checks the BUILT artifacts afterwards.
"""

from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("pypi_readme", REPO / "scripts" / "pypi_readme.py")
pypi_readme = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pypi_readme)

RAW = "https://raw.githubusercontent.com/DareDev256/fcp-mcp-server/v9.9.9/"
BLOB = "https://github.com/DareDev256/fcp-mcp-server/blob/v9.9.9/"


def test_the_real_readme_has_relative_targets_to_rewrite():
    """If this ever reads zero, the README went absolute by hand and the
    rewrite is no longer what keeps PyPI's images working."""
    text = (REPO / "README.md").read_text(encoding="utf-8")
    assert any(pypi_readme.is_media(t) for t in pypi_readme.relative_targets(text))


def test_the_rewritten_readme_has_no_relative_target():
    text = (REPO / "README.md").read_text(encoding="utf-8")
    new = pypi_readme.rewrite(text, "v9.9.9")
    assert pypi_readme.relative_targets(new) == []
    # every picture the README shows from the repo is now a raw URL pinned to the tag
    pictures = [t for t in pypi_readme.relative_targets(text) if pypi_readme.is_media(t)]
    assert pictures and all(RAW + t.removeprefix("./") in pypi_readme.media_urls(new) for t in pictures)
    assert "mcp-name: io.github.DareDev256/" in new


@pytest.mark.parametrize("before,after", [
    ("![hero](docs/assets/hero-still.png)", f"![hero]({RAW}docs/assets/hero-still.png)"),
    ("[![a](docs/a.gif)](docs/a.mp4)", f"[![a]({RAW}docs/a.gif)]({RAW}docs/a.mp4)"),
    ("see [the audit](docs/AUDIT.md#two)", f"see [the audit]({BLOB}docs/AUDIT.md#two)"),
    ('![x](./docs/x.png "title")', f'![x]({RAW}docs/x.png "title")'),
    ('<img src="docs/assets/logo.svg" width="72">', f'<img src="{RAW}docs/assets/logo.svg" width="72">'),
    ("[PyPI](https://pypi.org/project/fcp-mcp-server/)", "[PyPI](https://pypi.org/project/fcp-mcp-server/)"),
    ("[up](#compatibility) [mail](mailto:a@b.c)", "[up](#compatibility) [mail](mailto:a@b.c)"),
])
def test_rewrite(before, after):
    assert pypi_readme.rewrite(before, "v9.9.9") == after


def test_fenced_code_is_left_alone():
    text = "```json\n{\"a\": \"](docs/x.png)\"}\n```\n![x](docs/x.png)\n"
    new = pypi_readme.rewrite(text, "v9.9.9")
    assert '{"a": "](docs/x.png)"}' in new and f"![x]({RAW}docs/x.png)" in new


def _wheel(tmp_path, description: str) -> Path:
    whl = tmp_path / "fcp_mcp_server-9.9.9-py3-none-any.whl"
    with zipfile.ZipFile(whl, "w") as z:
        z.writestr("fcp_mcp_server-9.9.9.dist-info/METADATA",
                   "Metadata-Version: 2.1\nName: fcp-mcp-server\nVersion: 9.9.9\n"
                   "Description-Content-Type: text/markdown\n\n" + description)
    return whl


def test_check_passes_a_rewritten_artifact(tmp_path):
    body = pypi_readme.rewrite("![h](docs/h.png)\n`mcp-name: io.github.DareDev256/x`\n", "v9.9.9")
    assert pypi_readme.check([_wheel(tmp_path, body)], fetch=False) == []


def test_check_goes_red_on_a_relative_image(tmp_path):
    """Mutation check: one relative image path back in the built description fails the release."""
    body = "![h](docs/assets/hero-still.png)\n`mcp-name: io.github.DareDev256/x`\n"
    problems = pypi_readme.check([_wheel(tmp_path, body)], fetch=False)
    assert problems and "relative target 'docs/assets/hero-still.png'" in problems[0]


def test_check_goes_red_without_the_registry_marker(tmp_path):
    problems = pypi_readme.check([_wheel(tmp_path, "nothing here\n")], fetch=False)
    assert any("registry ownership marker" in p for p in problems)


def test_check_goes_red_on_an_image_that_does_not_answer_200(tmp_path, monkeypatch):
    monkeypatch.setattr(pypi_readme, "status", lambda url: 404)
    body = f"![h]({RAW}docs/missing.png)\n`mcp-name: io.github.DareDev256/x`\n"
    problems = pypi_readme.check([_wheel(tmp_path, body)], fetch=True)
    assert problems and "answered 404" in problems[0]


def test_the_publish_workflow_rewrites_before_building_and_checks_after():
    wf = (REPO / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")
    rewrite, build, check = (wf.index(s) for s in (
        "scripts/pypi_readme.py rewrite", "python -m build", "scripts/pypi_readme.py check"))
    assert rewrite < build < check
    assert "--fetch" in wf[check:check + 200]
