"""The shipped skill must not drift from the server's actual tool surface."""
import re
from pathlib import Path

import server

SKILL = Path(__file__).parent.parent / "skill" / "SKILL.md"


class TestSkillFrontmatter:
    def test_skill_file_exists(self):
        assert SKILL.exists()

    def test_has_name_and_description(self):
        text = SKILL.read_text()
        assert text.startswith("---\n")
        fm = text.split("---")[1]
        assert re.search(r"^name:\s*\S+", fm, re.M)
        assert re.search(r"^description:\s*\S+", fm, re.M)


class TestSkillMatchesServer:
    def test_every_group_is_documented(self):
        """A group the skill never mentions is a group the model will not use."""
        text = SKILL.read_text()
        for group in server.TOOL_GROUPS:
            assert f"`{group}`" in text, f"skill does not document group: {group}"

    def test_skill_names_no_action_the_server_lacks(self):
        """Catches the skill drifting ahead of, or behind, the code.

        The naive version of this test (grep every backticked lowercase
        `word_word` token and demand it be a known action or group) fires on
        ordinary technical prose that also happens to contain an underscore:
        `read_resource`, `frame_rate`, `asset_clip`, `test_skill.py`, and
        friends. None of those are things the model could call, so failing
        on them is a false positive, not a caught drift.

        Real actions are all shaped `<verb>_<...>`, and the finite set of
        verbs is derived straight from server.TOOL_HANDLERS rather than
        hand-maintained here (so it can't itself drift). A backticked token
        only has to resolve to a known action/group when its own leading
        verb matches one already in use by a real action — that is the
        precise slice of "looks like a tool action" this test cares about.
        Anything else (attribute names, resource schemes, file names) is
        prose and is skipped.
        """
        text = SKILL.read_text()
        known = set(server.TOOL_HANDLERS) | set(server.TOOL_GROUPS)
        known_verbs = {name.split("_", 1)[0] for name in server.TOOL_HANDLERS}
        for match in re.findall(r"`([a-z][a-z0-9_]{3,})`", text):
            if match in known or "_" not in match:
                continue
            verb = match.split("_", 1)[0]
            if verb not in known_verbs:
                continue
            assert match in known, f"skill references unknown action: {match}"
