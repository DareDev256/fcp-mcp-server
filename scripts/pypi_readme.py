"""Make the README PyPI publishes show its images.

PyPI renders the long description with no notion of the repository, so a
relative ``docs/assets/hero.gif`` is a broken image there. Until 0.31 every
image on https://pypi.org/project/fcp-mcp-server/ was broken: the hero, the
live-Final-Cut proof, both screenshots. GitHub resolves the same paths
fine, so the README in the repository keeps them relative and the publish
workflow rewrites a copy at release time, pinned to the release tag, so the
page for 0.31.0 shows 0.31.0's pictures forever.

    python scripts/pypi_readme.py rewrite --ref v0.31.0            # in place, before `python -m build`
    python scripts/pypi_readme.py check dist/*.whl dist/*.tar.gz --fetch

``rewrite`` turns every relative target into an absolute one: media
(images, GIF, MP4) to ``raw.githubusercontent.com/<repo>/<ref>/<path>`` and
everything else to ``github.com/<repo>/blob/<ref>/<path>``. Fenced code is
left alone. ``check`` reads the long description out of the BUILT
artifacts (what PyPI will actually receive), fails on any relative target,
and with ``--fetch`` fails on any image URL that does not answer 200.
"""

from __future__ import annotations

import argparse
import email
import re
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

REPO_SLUG = "DareDev256/fcp-mcp-server"
MEDIA = (".png", ".gif", ".jpg", ".jpeg", ".svg", ".webp", ".mp4", ".mov")

# `](target` — every inline markdown link or image target, including the
# outer link of a nested `[![alt](img)](link)`.
MD_TARGET = re.compile(r"\]\((?P<target>[^)\s]+)(?P<title>\s+\"[^\"]*\")?\)")
# src= / href= / srcset= in raw HTML.
HTML_TARGET = re.compile(r"""(?P<attr>\b(?:src|href|srcset))=(?P<q>["'])(?P<target>[^"']+)(?P=q)""")
FENCE = re.compile(r"^(```|~~~)")


def is_relative(target: str) -> bool:
    return not re.match(r"^(?:[a-z][a-z0-9+.-]*:|#|//)", target, re.I)


def is_media(target: str) -> bool:
    return target.split("#")[0].split("?")[0].lower().endswith(MEDIA)


def absolute(target: str, ref: str, slug: str = REPO_SLUG) -> str:
    path = target[2:] if target.startswith("./") else target
    if is_media(path):
        return f"https://raw.githubusercontent.com/{slug}/{ref}/{path}"
    return f"https://github.com/{slug}/blob/{ref}/{path}"


def _outside_fences(text: str):
    """Yield (is_code, chunk) so fenced blocks pass through untouched."""
    chunk, code = [], False
    for line in text.splitlines(keepends=True):
        if FENCE.match(line.lstrip()):
            if not code:
                yield False, "".join(chunk)
                chunk, code = [line], True
            else:
                chunk.append(line)
                yield True, "".join(chunk)
                chunk, code = [], False
            continue
        chunk.append(line)
    yield code, "".join(chunk)


def rewrite(text: str, ref: str, slug: str = REPO_SLUG) -> str:
    out = []
    for code, chunk in _outside_fences(text):
        if not code:
            chunk = MD_TARGET.sub(
                lambda m: f"]({absolute(m['target'], ref, slug) if is_relative(m['target']) else m['target']}"
                          f"{m['title'] or ''})", chunk)
            chunk = HTML_TARGET.sub(
                lambda m: f"{m['attr']}={m['q']}"
                          f"{absolute(m['target'], ref, slug) if is_relative(m['target']) else m['target']}{m['q']}",
                chunk)
        out.append(chunk)
    return "".join(out)


def relative_targets(text: str) -> list[str]:
    """Every relative link or image target left outside fenced code."""
    found = []
    for code, chunk in _outside_fences(text):
        if code:
            continue
        found += [m["target"] for m in MD_TARGET.finditer(chunk) if is_relative(m["target"])]
        found += [m["target"] for m in HTML_TARGET.finditer(chunk) if is_relative(m["target"])]
    return found


def media_urls(text: str) -> list[str]:
    urls = [m["target"] for m in MD_TARGET.finditer(text)] + [m["target"] for m in HTML_TARGET.finditer(text)]
    return sorted({u for u in urls if u.startswith("https://") and is_media(u)})


def long_description(artifact: Path) -> str:
    """The description PyPI will render, read from a built wheel or sdist."""
    if artifact.suffix == ".whl":
        with zipfile.ZipFile(artifact) as z:
            name = next(n for n in z.namelist() if n.endswith(".dist-info/METADATA"))
            raw = z.read(name).decode("utf-8")
    else:
        with tarfile.open(artifact) as t:
            member = next(m for m in t.getmembers() if m.name.count("/") == 1 and m.name.endswith("/PKG-INFO"))
            raw = t.extractfile(member).read().decode("utf-8")
    return email.message_from_string(raw).get_payload()


def status(url: str) -> int:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "fcp-mcp-server release check"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except OSError:
        return 0


def check(artifacts: list[Path], fetch: bool) -> list[str]:
    problems = []
    for artifact in artifacts:
        text = long_description(artifact)
        problems += [f"{artifact.name}: relative target {t!r}" for t in relative_targets(text)]
        if "mcp-name: io.github.DareDev256/" not in text:
            problems.append(f"{artifact.name}: the MCP registry ownership marker is missing")
        if fetch:
            for url in media_urls(text):
                code = status(url)
                if code != 200:
                    problems.append(f"{artifact.name}: {url} answered {code or 'nothing'}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    rw = sub.add_parser("rewrite")
    rw.add_argument("--ref", required=True, help="git ref the URLs pin to, e.g. v0.31.0")
    rw.add_argument("--path", default="README.md")
    ck = sub.add_parser("check")
    ck.add_argument("artifacts", nargs="+", type=Path)
    ck.add_argument("--fetch", action="store_true", help="also require every image URL to answer 200")
    args = parser.parse_args(argv)

    if args.cmd == "rewrite":
        path = Path(args.path)
        new = rewrite(path.read_text(encoding="utf-8"), args.ref)
        left = relative_targets(new)
        if left:
            print(f"relative targets survived the rewrite: {left}", file=sys.stderr)
            return 1
        path.write_text(new, encoding="utf-8")
        print(f"{path}: {len(media_urls(new))} media URLs pinned to {args.ref}")
        return 0

    problems = check(args.artifacts, args.fetch)
    for p in problems:
        print(p, file=sys.stderr)
    if not problems:
        print(f"{len(args.artifacts)} artifact(s): no relative targets" + (", every image answers 200" if args.fetch else ""))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
