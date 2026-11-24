"""Elicitation and MCP tasks (0.36): the person answers, long work runs in the background.

Every gate that asks keeps its old refusal when nobody can be asked, and
every answer is the PERSON's: the model's arguments cannot supply one. Each
route is driven through the real code: a handshake-era session (both SDK
generations), the 2026-07-28 InputRequiredResult round trip and the tasks
extension through the registered handlers (2.x), and the whole thing over
stdio against the real runner (2.x).
"""
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import server
import tools.gen as gen_tool
from fcpxml import gen, interactive, journal, mcp_compat, progress
from fcpxml.mcp_compat import is_legacy_api

REPO = Path(__file__).resolve().parent.parent
SAMPLE = REPO / "examples" / "sample.fcpxml"
MODERN = interactive.MODERN_PROTOCOL_VERSION
needs_v2 = pytest.mark.skipif(is_legacy_api(), reason="the 2026-07-28 era needs mcp 2.x")


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "sample.fcpxml"
    shutil.copy(SAMPLE, p)
    return p


class FakeSession:
    """A handshake-era session whose client answers every form with *reply*."""

    def __init__(self, reply, caps=None):
        self.reply = reply
        self.client_capabilities = caps if caps is not None else {"elicitation": {}}
        self.asked: list[tuple[str, dict]] = []

    async def elicit_form(self, message, schema, related_request_id=None):
        self.asked.append((message, schema))
        return self.reply


def _legacy_ctx(reply, caps=None):
    return SimpleNamespace(session=FakeSession(reply, caps), meta=None, request_id=7)


def _call(ctx, name, arguments):
    async def go():
        token = mcp_compat._REQUEST_CTX.set(ctx)
        try:
            return await server.call_tool(name, arguments)
        finally:
            mcp_compat._REQUEST_CTX.reset(token)
    return asyncio.run(go())


def _export(project, **extra):
    return {"action": "export_csv", "args": {"filepath": str(project), **extra}}


# ---------------------------------------------------------------------------
# The review gate, handshake era (both SDKs)
# ---------------------------------------------------------------------------

def test_the_person_ships_it_unreviewed_and_the_reply_says_who_decided(project):
    ctx = _legacy_ctx({"action": "accept", "content": {"decision": "ship"}})
    text = _call(ctx, "deliver", _export(project))[0].text
    assert "approved by the user in the review dialog" in text
    assert not text.startswith("Refused:")
    (message, schema), = ctx.session.asked
    assert "export_csv" in message and project.name in message
    assert schema["required"] == ["decision"]


def test_the_person_chooses_review_and_nothing_ships(project):
    ctx = _legacy_ctx({"action": "accept", "content": {"decision": "review"}})
    text = _call(ctx, "deliver", _export(project))[0].text
    assert text.startswith("Not shipped: the review gate asked the user in a dialog")
    assert "Stop. I will watch a preview_render first" in text and '"preview_render"' in text
    assert list(project.parent.glob("*.csv")) == []


@pytest.mark.parametrize("action", ["decline", "cancel"])
def test_a_declined_or_dismissed_dialog_ships_nothing(project, action):
    text = _call(_legacy_ctx({"action": action}), "deliver", _export(project))[0].text
    assert text.startswith("Not shipped: the review gate asked the user in a dialog and they declined it")


def test_a_client_without_elicitation_keeps_the_old_refusal(project):
    ctx = _legacy_ctx({"action": "accept", "content": {"decision": "ship"}}, caps={})
    text = _call(ctx, "deliver", _export(project))[0].text
    assert text.startswith("Refused:") and "confirm_unreviewed" in text
    assert ctx.session.asked == []


def test_no_request_at_all_keeps_the_old_refusal(project):
    text = asyncio.run(server.call_tool("deliver", _export(project)))[0].text
    assert text.startswith("Refused:")


def test_a_reviewed_file_is_never_asked_about(project):
    tok = journal.begin("preview", "preview_render", {"filepath": str(project)}, str(project))
    proxy = project.parent / "sample_proxy.mp4"
    proxy.write_bytes(b"mp4")
    journal.note_output(str(proxy))
    journal.finish(tok)
    ctx = _legacy_ctx({"action": "accept", "content": {"decision": "ship"}})
    text = _call(ctx, "deliver", _export(project))[0].text
    assert ctx.session.asked == [] and "UNREVIEWED" not in text


def test_the_flag_still_ships_without_a_dialog_and_is_stamped_as_the_flag(project):
    ctx = _legacy_ctx({"action": "accept", "content": {"decision": "ship"}})
    text = _call(ctx, "deliver", _export(project, confirm_unreviewed=True))[0].text
    assert ctx.session.asked == []
    assert "confirm_unreviewed=true" in text and "review dialog" not in text


def test_the_gate_check_goes_red_if_ask_is_skipped(project, monkeypatch):
    """Mutation: a gate that never asks leaves the refusal in place, which is
    what the 'ships' test above would see. The dialog is what moved it."""
    async def never(*a, **k):
        return interactive.UNAVAILABLE
    monkeypatch.setattr(interactive, "ask", never)
    ctx = _legacy_ctx({"action": "accept", "content": {"decision": "ship"}})
    assert _call(ctx, "deliver", _export(project))[0].text.startswith("Refused:")


# ---------------------------------------------------------------------------
# The library picker for push_to_fcp
# ---------------------------------------------------------------------------

LIBS = [{"name": "Gym", "file": "/Users/x/Movies/Gym.fcpbundle", "events": []},
        {"name": "Client", "file": "/Users/x/Movies/Client.fcpbundle", "events": []}]


def _patch_libs(monkeypatch, libs, running=True):
    import fcpxml.live as live
    monkeypatch.setattr(live, "fcp_is_running", lambda: running)
    monkeypatch.setattr(live, "list_fcp_libraries", lambda allow_launch=False: libs)


def _answer(monkeypatch, answer):
    seen = []

    async def fake(key, message, schema):
        seen.append((key, message, schema))
        return answer
    monkeypatch.setattr(interactive, "ask", fake)
    return seen


def test_two_open_libraries_ask_which_and_return_its_path(monkeypatch, project):
    _patch_libs(monkeypatch, LIBS)
    seen = _answer(monkeypatch, interactive.Answer("accept", {"library": LIBS[1]["file"]}))
    assert asyncio.run(server._ask_library(str(project))) == LIBS[1]["file"]
    (key, message, schema), = seen
    assert "2 libraries" in message
    assert [o["const"] for o in schema["properties"]["library"]["oneOf"]] == [lib["file"] for lib in LIBS]


def test_one_open_library_is_not_a_question(monkeypatch, project):
    _patch_libs(monkeypatch, LIBS[:1])
    seen = _answer(monkeypatch, interactive.Answer("accept", {"library": LIBS[0]["file"]}))
    assert asyncio.run(server._ask_library(str(project))) is None and seen == []


def test_a_library_the_dialog_did_not_offer_is_refused(monkeypatch, project):
    _patch_libs(monkeypatch, LIBS)
    _answer(monkeypatch, interactive.Answer("accept", {"library": "/tmp/Elsewhere.fcpbundle"}))
    with pytest.raises(ValueError, match="no library was chosen"):
        asyncio.run(server._ask_library(str(project)))


def test_no_dialog_leaves_the_named_refusal_to_resolve_library_target(monkeypatch, project):
    _patch_libs(monkeypatch, LIBS)
    _answer(monkeypatch, interactive.UNAVAILABLE)
    assert asyncio.run(server._ask_library(str(project))) is None


# ---------------------------------------------------------------------------
# The money gate
# ---------------------------------------------------------------------------

def _priced_job():
    for (provider, model), entry in gen.PRICES.items():
        try:
            q = gen._build_quote(provider, 4, "a city street at night", model)
        except gen.GenError:
            continue
        if q.usd is not None and not q.over_cap and q.available:
            return provider, model
    pytest.skip("no priced, available provider in the table")


def test_an_approved_spend_returns_a_quote_the_gate_accepts(monkeypatch):
    provider, model = _priced_job()
    seen = _answer(monkeypatch, interactive.Answer("accept", {"decision": "spend"}))
    qid = asyncio.run(gen_tool._ask_spend(provider, 4, "a city street at night", model, "B-roll over 'X'"))
    q = gen.confirm_quote(qid, True, provider, 4, "a city street at night", model)
    assert q.id == qid
    (_key, message, _schema), = seen
    assert f"US${q.usd}" in message and "a city street at night" in message


def test_an_approval_buys_only_the_job_it_showed(monkeypatch):
    provider, model = _priced_job()
    _answer(monkeypatch, interactive.Answer("accept", {"decision": "spend"}))
    qid = asyncio.run(gen_tool._ask_spend(provider, 4, "a city street at night", model, "x"))
    with pytest.raises(gen.GenError):
        gen.confirm_quote(qid, True, provider, 4, "a different prompt", model)


def test_a_declined_spend_refuses_before_anything_is_sent(monkeypatch):
    provider, model = _priced_job()
    _answer(monkeypatch, interactive.Answer("accept", {"decision": "stop"}))
    with pytest.raises(gen.RefusedError, match="did not approve it"):
        asyncio.run(gen_tool._ask_spend(provider, 4, "a city street at night", model, "x"))


def test_nobody_to_ask_leaves_the_quote_then_confirm_refusal(monkeypatch):
    provider, model = _priced_job()
    _answer(monkeypatch, interactive.UNAVAILABLE)
    assert asyncio.run(gen_tool._ask_spend(provider, 4, "a city street at night", model, "x")) is None


# ---------------------------------------------------------------------------
# requestState and answers
# ---------------------------------------------------------------------------

def test_sealed_state_round_trips_and_refuses_tampering():
    token = interactive.seal_state({"review_gate": {"action": "accept", "content": {"decision": "ship"}}})
    assert interactive.unseal_state(token)["review_gate"]["content"] == {"decision": "ship"}
    flipped = token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1]
    assert interactive.unseal_state(flipped) == {}
    assert interactive.unseal_state("not-a-token") == {}
    assert interactive.unseal_state(None) == {}


def test_stale_state_is_dropped(monkeypatch):
    token = interactive.seal_state({"k": {"action": "accept", "content": {}}})
    real = time.time
    monkeypatch.setattr(interactive.time, "time", lambda: real() + interactive._STATE_TTL_S + 1)
    assert interactive.unseal_state(token) == {}


def test_answers_merge_earlier_rounds_with_this_retry():
    state = interactive.seal_state({"review_gate": {"action": "accept", "content": {"decision": "ship"}}})
    merged = interactive.answers_for_call(state, {"library": {"action": "accept", "content": {"library": "/a"}}})
    assert set(merged) == {"review_gate", "library"}


@needs_v2
def test_the_choice_form_is_a_spec_valid_elicitation_schema():
    from mcp.server.elicitation import _validate_rendered_properties
    schema = interactive.choice_schema("decision", "Spend", [("spend", "Spend"), ("stop", "Stop")])
    _validate_rendered_properties(schema)


# ---------------------------------------------------------------------------
# 2026-07-28 through the registered handlers (2.x)
# ---------------------------------------------------------------------------

def _modern_ctx(caps):
    return SimpleNamespace(
        protocol_version=MODERN, session=None, request_id=1,
        meta={interactive.CLIENT_CAPABILITIES_META_KEY: caps},
    )


def _handler(method):
    entry = server.server._request_handlers[method]
    return getattr(entry, "handler", entry)


def _dump(result):
    if isinstance(result, dict):
        return result
    return json.loads(result.model_dump_json(by_alias=True, exclude_none=True))


@needs_v2
def test_a_modern_call_asks_with_input_required_then_ships_on_the_retry(project):
    import mcp.types as types
    ctx = _modern_ctx({"elicitation": {"form": {}}})
    call = _handler("tools/call")
    params = types.CallToolRequestParams(name="deliver", arguments=_export(project))
    first = _dump(asyncio.run(call(ctx, params)))
    assert first["resultType"] == "input_required"
    request = first["inputRequests"]["review_gate"]
    assert request["method"] == "elicitation/create" and request["params"]["mode"] == "form"
    retry = types.CallToolRequestParams.model_validate({
        "name": "deliver", "arguments": _export(project),
        "inputResponses": {"review_gate": {"action": "accept", "content": {"decision": "ship"}}},
        "requestState": first["requestState"],
    })
    text = _dump(asyncio.run(call(ctx, retry)))["content"][0]["text"]
    assert "approved by the user in the review dialog" in text


@needs_v2
def test_a_modern_client_without_elicitation_gets_the_refusal(project):
    import mcp.types as types
    out = _dump(asyncio.run(_handler("tools/call")(
        _modern_ctx({}), types.CallToolRequestParams(name="deliver", arguments=_export(project)))))
    assert out["content"][0]["text"].startswith("Refused:")


@needs_v2
def test_a_task_runs_in_the_background_asks_and_resumes(project):
    import mcp.types as types
    ctx = _modern_ctx({"elicitation": {"form": {}}, "extensions": {interactive.TASKS_EXTENSION_ID: {}}})
    args = {"action": "export_role_stems", "args": {"filepath": str(project)}}

    async def go():
        created = _dump(await _handler("tools/call")(ctx, types.CallToolRequestParams(name="deliver", arguments=args)))
        assert created["resultType"] == "task" and created["status"] == "working"
        get = _handler("tasks/get")
        get_params = server.server._request_handlers["tasks/get"].params_type
        tid = created["taskId"]
        for _ in range(100):
            state = await get(None, get_params(task_id=tid))
            if state["status"] != "working":
                break
            await asyncio.sleep(0.02)
        assert state["status"] == "input_required" and "review_gate" in state["inputRequests"]
        upd_params = server.server._request_handlers["tasks/update"].params_type
        await _handler("tasks/update")(None, upd_params(task_id=tid, input_responses={
            "review_gate": {"action": "accept", "content": {"decision": "review"}}}))
        for _ in range(100):
            state = await get(None, get_params(task_id=tid))
            if state["status"] in ("completed", "failed", "cancelled"):
                break
            await asyncio.sleep(0.02)
        return state

    state = asyncio.run(go())
    assert state["status"] == "completed"
    assert state["result"]["content"][0]["text"].startswith("Not shipped")


@needs_v2
def test_only_long_actions_become_tasks(project):
    import mcp.types as types
    ctx = _modern_ctx({"extensions": {interactive.TASKS_EXTENSION_ID: {}}})
    out = _dump(asyncio.run(_handler("tools/call")(ctx, types.CallToolRequestParams(
        name="inspect", arguments={"action": "analyze_timeline", "args": {"filepath": str(project)}}))))
    assert out.get("resultType", "complete") == "complete" and out["content"]


@needs_v2
def test_a_client_that_did_not_opt_in_never_gets_a_task(project):
    import mcp.types as types
    out = _dump(asyncio.run(_handler("tools/call")(_modern_ctx({}), types.CallToolRequestParams(
        name="preview", arguments={"action": "preview_check", "args": {"filepath": str(project)}}))))
    assert out.get("resultType", "complete") != "task"


def test_progress_inside_a_task_becomes_its_status_line():
    record = interactive.TaskRecord(task_id="t")
    token = interactive._TASK.set(record)
    try:
        p = progress.start(4)
        asyncio.run(p.step("Transcribing clip.mov"))
    finally:
        interactive._TASK.reset(token)
    assert record.status_message == "Transcribing clip.mov (1/4)"


# ---------------------------------------------------------------------------
# The whole thing over stdio, against the real 2.x runner
# ---------------------------------------------------------------------------

@needs_v2
def test_over_stdio_a_modern_client_gets_skills_tasks_and_questions(tmp_path):
    cut = tmp_path / "cut.fcpxml"
    shutil.copy(SAMPLE, cut)
    env = dict(os.environ, FCP_PROJECTS_DIRS=str(tmp_path), FCP_MCP_JOURNAL=str(tmp_path / "journal"),
               FCP_MCP_INDEX="off", FCP_MCP_AUTOPUSH="0")
    proc = subprocess.Popen([sys.executable, str(REPO / "server.py")], cwd=tmp_path, env=env, text=True,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    ids = iter(range(1, 10_000))

    def rpc(method, params=None, caps=None):
        rid = next(ids)
        p = dict(params or {})
        p["_meta"] = {
            "io.modelcontextprotocol/protocolVersion": MODERN,
            "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "0"},
            interactive.CLIENT_CAPABILITIES_META_KEY: caps or {},
        }
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": p}) + "\n")
        proc.stdin.flush()
        while True:
            line = proc.stdout.readline()
            assert line, proc.stderr.read()[-2000:]
            msg = json.loads(line)
            if msg.get("id") == rid:
                return msg

    try:
        found = rpc("server/discover")["result"]
        ext = found["capabilities"]["extensions"]
        assert interactive.TASKS_EXTENSION_ID in ext
        assert ext["io.modelcontextprotocol/skills"] == {"directoryRead": True}
        assert "skill://rough-cut/SKILL.md" in found["instructions"]

        listed = rpc("skills/list")["result"]
        assert listed["resultType"] == "complete" and listed["cacheScope"] == "public"
        for entry in listed["skills"]:
            for res in entry["resources"]:
                body = rpc("resources/read", {"uri": res["uri"]})["result"]["contents"][0]["text"].encode()
                assert ("sha256:" + hashlib.sha256(body).hexdigest(), len(body)) == (res["digest"], res["size"])
        assert rpc("skills/get", {"uri": "skill://nope/SKILL.md"})["error"]["code"] == -32602

        caps = {"elicitation": {"form": {}}, "extensions": {interactive.TASKS_EXTENSION_ID: {}}}
        asked = rpc("tools/call", {"name": "deliver", "arguments": _export(cut)}, {"elicitation": {"form": {}}})
        assert asked["result"]["resultType"] == "input_required"

        task = rpc("tools/call", {"name": "preview",
                                  "arguments": {"action": "preview_check", "args": {"filepath": str(cut)}}}, caps)
        assert task["result"]["resultType"] == "task"
        for _ in range(200):
            state = rpc("tasks/get", {"taskId": task["result"]["taskId"]}, caps)["result"]
            if state["status"] in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.05)
        assert state["status"] == "completed" and state["result"]["content"]
        assert rpc("tasks/get", {"taskId": "nope"}, caps)["error"]["code"] == -32602
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)
