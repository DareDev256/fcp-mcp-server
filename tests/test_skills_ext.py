"""Skills over MCP (0.36): the workflow skills are served whole, verifiable and true.

A served skill is a promise to a host that verifies digests before it acts:
every file listed, every digest the digest of the bytes a read returns, the
frontmatter the file's own. And a skill that names an action this server
does not have is worse than none, so every action a skill names is checked
against the live tool table.
"""
import ast
import asyncio
import fnmatch
import hashlib
import json
import re
from pathlib import Path

import pytest

import server
from fcpxml import skills_ext

ROOT = Path(__file__).resolve().parent.parent
SKILL_DIRS = sorted(p for p in (ROOT / "fcpxml" / "skills").iterdir() if p.is_dir())


@pytest.fixture
def fresh_catalogue():
    skills_ext.catalogue.cache_clear()
    yield
    skills_ext.catalogue.cache_clear()


def test_every_skill_directory_is_served():
    served = {s.name for s in skills_ext.catalogue()}
    assert served == {p.name for p in SKILL_DIRS}
    assert len(served) >= 6


def test_each_entry_is_a_complete_manifest_of_the_bytes_on_disk():
    for skill in skills_ext.catalogue():
        entry = skill.entry()
        assert entry["uri"] == f"skill://{skill.name}/SKILL.md"
        on_disk = {
            f"skill://{skill.name}/{p.relative_to(ROOT / 'fcpxml' / 'skills' / skill.name).as_posix()}": p.read_bytes()
            for p in (ROOT / "fcpxml" / "skills" / skill.name).rglob("*") if p.is_file()
        }
        listed = {r["uri"]: r for r in entry["resources"]}
        assert set(listed) == set(on_disk)
        for uri, data in on_disk.items():
            assert listed[uri]["digest"] == "sha256:" + hashlib.sha256(data).hexdigest()
            assert listed[uri]["size"] == len(data)


def test_frontmatter_is_the_files_own_and_inside_the_limits():
    for skill in skills_ext.catalogue():
        text = (ROOT / "fcpxml" / "skills" / skill.name / "SKILL.md").read_text()
        assert skills_ext.parse_frontmatter(text) == skill.frontmatter
        assert skill.frontmatter["name"] == skill.name
        assert 0 < len(skill.frontmatter["description"]) <= 1024
        # Claude Code's limits for served skills: 100 KB a text file, 400 KB a skill.
        assert all(len(f.data) <= 100_000 for f in skill.files)
        assert sum(len(f.data) for f in skill.files) <= 400_000


def _group_actions() -> dict[str, set[str]]:
    out = {}
    for group, spec in server.TOOL_GROUPS.items():
        actions = spec.get("actions") if isinstance(spec, dict) else spec
        out[group] = set(actions)
    return out


def test_every_action_a_skill_names_exists():
    """A skill that tells the agent to call an action the server lacks sends it
    into a refusal it cannot recover from. Checked against the live table."""
    every_action = set().union(*_group_actions().values())
    for skill in skills_ext.catalogue():
        body = skill.files[[f.rel for f in skill.files].index("SKILL.md")].data.decode()
        named = set(re.findall(r'"action"\s*:\s*"([a-z_0-9]+)"', body))
        assert named, f"{skill.name} names no action"
        assert named <= every_action, f"{skill.name}: unknown {sorted(named - every_action)}"


def test_the_action_check_goes_red_on_an_invented_action():
    every_action = set().union(*_group_actions().values())
    planted = '{"action": "export_to_tiktok"}'
    assert not set(re.findall(r'"action"\s*:\s*"([a-z_0-9]+)"', planted)) <= every_action


@pytest.mark.parametrize("bad", [
    "no frontmatter at all\n",
    "---\nname: x\ndescription: >\n  folded\n---\n",
    "---\nname: x\nmetadata:\n  nested: 1\n---\n",
    "---\nname: x\ntags: [a, b]\n---\n",
])
def test_frontmatter_this_reader_cannot_reproduce_exactly_is_refused(bad):
    assert skills_ext.parse_frontmatter(bad) is None


def _plant(tmp_path, monkeypatch, dirname, text, extra=None):
    root = tmp_path / "skills"
    (root / dirname).mkdir(parents=True)
    (root / dirname / "SKILL.md").write_text(text)
    for rel, body in (extra or {}).items():
        target = root / dirname / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    monkeypatch.setattr(skills_ext, "SKILLS_ROOT", root)


def test_a_name_that_does_not_match_its_directory_is_not_served(tmp_path, monkeypatch, fresh_catalogue):
    _plant(tmp_path, monkeypatch, "rough-cut", "---\nname: roughcut\ndescription: d\n---\nbody\n")
    assert skills_ext.catalogue() == ()


def test_a_bad_name_is_not_served(tmp_path, monkeypatch, fresh_catalogue):
    _plant(tmp_path, monkeypatch, "Rough_Cut", "---\nname: Rough_Cut\ndescription: d\n---\nbody\n")
    assert skills_ext.catalogue() == ()


def test_nested_files_are_listed_and_directories_read(tmp_path, monkeypatch, fresh_catalogue):
    _plant(tmp_path, monkeypatch, "demo", "---\nname: demo\ndescription: \"a: b\"\n---\nsee references/A.md\n",
           {"references/A.md": "a", "references/deep/B.md": "b", ".hidden": "x"})
    (skill,) = skills_ext.catalogue()
    assert skill.frontmatter == {"name": "demo", "description": "a: b"}
    assert [f.rel for f in skill.files] == ["SKILL.md", "references/A.md", "references/deep/B.md"]
    root = skills_ext.directory_read("skill://demo")["resources"]
    assert {(r["name"], r["mimeType"]) for r in root} == {("SKILL.md", "text/markdown"),
                                                          ("references", "inode/directory")}
    refs = skills_ext.directory_read("skill://demo/references")["resources"]
    assert {r["uri"] for r in refs} == {"skill://demo/references/A.md", "skill://demo/references/deep"}
    for not_a_dir in ("skill://demo/SKILL.md", "skill://demo/nope", "skill://other"):
        with pytest.raises(skills_ext.SkillError):
            skills_ext.directory_read(not_a_dir)
    assert skills_ext.read_skill_file("skill://demo/references/deep/B.md") == ("b", "text/markdown")
    with pytest.raises(skills_ext.SkillError):
        skills_ext.read_skill_file("skill://demo/.hidden")


def test_get_answers_only_for_a_skill_md_uri():
    assert skills_ext.get_skill("skill://rough-cut/SKILL.md")["skill"]["frontmatter"]["name"] == "rough-cut"
    for uri in ("skill://rough-cut", "skill://rough-cut/other.md", "skill://nope/SKILL.md", "file:///x/SKILL.md"):
        with pytest.raises(skills_ext.SkillError):
            skills_ext.get_skill(uri)


def test_the_skills_are_plain_resources_too():
    listed = {str(r.uri): r for r in asyncio.run(server.list_resources())}
    for skill in skills_ext.catalogue():
        r = listed[skill.uri]
        assert r.name == skill.name and r.description == skill.frontmatter["description"]
    text = asyncio.run(server.read_resource("skill://rough-cut/SKILL.md"))[0]
    assert text.mime_type == "text/markdown" and text.content.startswith("---\nname: rough-cut\n")


def test_the_instructions_point_at_every_skill():
    for skill in skills_ext.catalogue():
        assert skill.uri in server.server.instructions


def test_every_skill_file_is_package_data():
    text = (ROOT / "pyproject.toml").read_text()
    block = re.search(r"\[tool\.setuptools\.package-data\](.*?)(?=\n\[|\Z)", text, re.S)
    patterns = ast.literal_eval(re.search(r"^fcpxml = (\[.*?\])", block.group(1), re.M | re.S).group(1))
    for path in (ROOT / "fcpxml" / "skills").rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            rel = path.relative_to(ROOT / "fcpxml").as_posix()
            assert any(fnmatch.fnmatch(rel, pat) and rel.count("/") == pat.count("/") for pat in patterns), rel


def test_list_result_carries_the_cache_fields():
    listed = skills_ext.list_skills()
    assert listed["ttlMs"] > 0 and listed["cacheScope"] == "public"
    json.dumps(listed)  # wire-serialisable as it stands
