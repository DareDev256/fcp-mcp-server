"""Skills over MCP: serve the workflow skills in ``fcpxml/skills/`` (0.36).

Implements the ``io.modelcontextprotocol/skills`` extension (beta revision,
github.com/modelcontextprotocol/ext-skills, specification/beta/skills.mdx):

- every file of a skill is a resource at ``skill://<name>/<path>``, read with
  the ordinary ``resources/read``;
- ``skills/list`` returns one entry per skill: its frontmatter verbatim and a
  COMPLETE manifest of its files with ``sha256:`` digests and byte sizes;
- ``skills/get`` returns one entry by the URI of its ``SKILL.md``;
- ``resources/directory/read`` lists a skill directory's direct children
  (declared as ``directoryRead: true``).

Claude Code (2.1.331+) loads each one as a ``/<server>:<skill>`` skill. A
client without the extension still sees the ``skill://`` files as plain
resources, and the server's instructions name the ``SKILL.md`` URIs.

The catalogue is read once from the installed package and cached: the files
are package data, so they cannot change under a running server, and a digest
computed once is the digest of every read. A skill whose frontmatter breaks
the Agent Skills rules is left out and logged, never served half-valid.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SKILLS_EXTENSION_ID = "io.modelcontextprotocol/skills"
SKILLS_ROOT = Path(__file__).resolve().parent / "skills"
SCHEME = "skill://"
DIRECTORY_MIME = "inode/directory"
LIST_TTL_MS = 5 * 60 * 1000

# agentskills.io: 1-64 chars, lowercase letters, digits and single hyphens.
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_DESCRIPTION_MAX = 1024

__all__ = [
    "SKILLS_EXTENSION_ID",
    "SkillError",
    "catalogue",
    "directory_read",
    "get_skill",
    "is_skill_uri",
    "list_skills",
    "read_skill_file",
    "skill_resources",
]


class SkillError(ValueError):
    """A request naming something this server does not serve (-32602)."""


@dataclass(frozen=True)
class SkillFile:
    rel: str           # path inside the skill, "/"-separated
    data: bytes

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.data).hexdigest()

    @property
    def mime(self) -> str:
        if self.rel.endswith(".md"):
            return "text/markdown"
        guessed, _ = mimetypes.guess_type(self.rel)
        return guessed or "text/plain"


@dataclass(frozen=True)
class Skill:
    name: str
    frontmatter: dict
    files: tuple[SkillFile, ...]

    @property
    def uri(self) -> str:
        return f"{SCHEME}{self.name}/SKILL.md"

    def file_uri(self, f: SkillFile) -> str:
        return f"{SCHEME}{self.name}/{f.rel}"

    def entry(self) -> dict:
        return {
            "uri": self.uri,
            "frontmatter": dict(self.frontmatter),
            "resources": [
                {"uri": self.file_uri(f), "digest": f.digest, "size": len(f.data)}
                for f in self.files
            ],
        }


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        inner = value[1:-1]
        return inner.replace('\\"', '"') if value[0] == '"' else inner.replace("''", "'")
    return value


def parse_frontmatter(text: str) -> Optional[dict]:
    """The YAML frontmatter of a SKILL.md as a dict of top-level scalars.

    The served skills are ours, and their frontmatter is flat ``key: value``
    lines. Anything this reader cannot represent exactly (a nested map, a
    block scalar, a list) returns None, so the skill is refused rather than
    served with frontmatter that differs from its file, which the spec
    forbids.
    """
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end < 0:
        return None
    out: dict = {}
    for line in text[4:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0].isspace() or ":" not in line:
            return None
        key, _, value = line.partition(":")
        value = value.strip()
        if not value or value[0] in "|>[{&*!" or value == "-":
            return None
        out[key.strip()] = _unquote(value)
    return out


def _load_skill(directory: Path) -> Optional[Skill]:
    skill_md = directory / "SKILL.md"
    if not skill_md.is_file():
        return None
    data = skill_md.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning("skill %s: SKILL.md is not UTF-8; not served", directory.name)
        return None
    fm = parse_frontmatter(text)
    if fm is None:
        logger.warning("skill %s: frontmatter is not flat key: value YAML; not served", directory.name)
        return None
    name, description = fm.get("name"), fm.get("description")
    if name != directory.name or not isinstance(name, str) or not _NAME_RE.match(name) or len(name) > 64:
        logger.warning("skill %s: name %r must match its directory and the naming rules", directory.name, name)
        return None
    if not isinstance(description, str) or not description.strip() or len(description) > _DESCRIPTION_MAX:
        logger.warning("skill %s: description missing or over %d characters", name, _DESCRIPTION_MAX)
        return None
    files = []
    for path in sorted(directory.rglob("*")):
        rel = path.relative_to(directory).as_posix()
        if not path.is_file() or any(part.startswith((".", "__")) for part in rel.split("/")):
            continue
        files.append(SkillFile(rel=rel, data=path.read_bytes()))
    return Skill(name=name, frontmatter=fm, files=tuple(files))


@lru_cache(maxsize=1)
def catalogue() -> tuple[Skill, ...]:
    if not SKILLS_ROOT.is_dir():
        return ()
    skills = []
    for directory in sorted(p for p in SKILLS_ROOT.iterdir() if p.is_dir()):
        skill = _load_skill(directory)
        if skill is not None:
            skills.append(skill)
    return tuple(skills)


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def is_skill_uri(uri: str) -> bool:
    return str(uri).startswith(SCHEME)


def _split(uri: str) -> tuple[str, str]:
    """``skill://<name>/<rest>`` -> (name, rest); rest is "" for the skill root."""
    body = str(uri)[len(SCHEME):]
    name, _, rest = body.partition("/")
    return name, rest.rstrip("/")


def _skill_named(name: str) -> Optional[Skill]:
    return next((s for s in catalogue() if s.name == name), None)


def list_skills() -> dict:
    return {
        "skills": [s.entry() for s in catalogue()],
        "ttlMs": LIST_TTL_MS,
        "cacheScope": "public",
    }


def get_skill(uri: str) -> dict:
    name, rest = _split(uri)
    skill = _skill_named(name) if is_skill_uri(uri) and rest == "SKILL.md" else None
    if skill is None:
        raise SkillError(f"No skill is served at {uri}")
    return {"skill": skill.entry(), "ttlMs": LIST_TTL_MS, "cacheScope": "public"}


def read_skill_file(uri: str) -> tuple[str, str]:
    """(text, mimeType) of one skill file. Raises SkillError for anything else."""
    name, rest = _split(uri)
    skill = _skill_named(name)
    f = next((f for f in skill.files if f.rel == rest), None) if skill and rest else None
    if f is None:
        raise SkillError(f"No skill file is served at {uri}")
    return f.data.decode("utf-8"), f.mime


def directory_read(uri: str) -> dict:
    """Direct children of a skill directory, files and subdirectories."""
    name, rest = _split(uri)
    skill = _skill_named(name) if is_skill_uri(uri) else None
    if skill is None:
        raise SkillError(f"{uri} is not a directory resource")
    prefix = f"{rest}/" if rest else ""
    if rest and not any(f.rel.startswith(prefix) for f in skill.files):
        raise SkillError(f"{uri} is not a directory resource")
    children: dict[str, dict] = {}
    for f in skill.files:
        if not f.rel.startswith(prefix):
            continue
        head, sep, _tail = f.rel[len(prefix):].partition("/")
        child_uri = f"{SCHEME}{name}/{prefix}{head}"
        if sep:
            children.setdefault(child_uri, {"uri": child_uri, "name": head, "mimeType": DIRECTORY_MIME})
        else:
            children[child_uri] = {"uri": child_uri, "name": head, "mimeType": f.mime}
    return {"resources": list(children.values()), "ttlMs": LIST_TTL_MS, "cacheScope": "public"}


def skill_resources() -> list[dict]:
    """``resources/list`` entries: each SKILL.md with its name and description,
    and every supporting file, so a client without the extension can read them."""
    out = []
    for skill in catalogue():
        for f in skill.files:
            entry = {"uri": skill.file_uri(f), "name": f"{skill.name}/{f.rel}", "mimeType": f.mime}
            if f.rel == "SKILL.md":
                entry["name"] = skill.name
                entry["description"] = skill.frontmatter["description"]
            out.append(entry)
    return out


def instructions_pointer() -> str:
    """One line for the server instructions naming every served skill."""
    names = [s.name for s in catalogue()]
    if not names:
        return ""
    uris = ", ".join(f"{SCHEME}{n}/SKILL.md" for n in names)
    return f"Workflow skills (read before a multi-step edit): {uris}."
