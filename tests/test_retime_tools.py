"""Exercise variable retiming through the real grouped/flat MCP boundary."""
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

import server
from fcpxml import journal
from fcpxml.mcp_compat import tool_input_schema
from tools import keyframes as kt
from tools import retime as rt

XML = '''<fcpxml version="1.13"><resources>
<format id="r1" frameDuration="1/30s" width="1280" height="720"/>
<asset id="r2" start="0s" duration="20s" hasVideo="1" hasAudio="1" audioSources="1" audioChannels="1" format="r1"><media-rep kind="original-media" src="file:///synthetic.mov"/></asset>
</resources><library><event name="E"><project name="Retime tests">
<sequence format="r1" duration="8s" tcStart="0s"><spine>
<asset-clip name="Duplicate" ref="r2" offset="0s" start="5s" duration="6s"><adjust-volume><param name="amount"><keyframeAnimation><keyframe time="5s" value="-24dB"/><keyframe time="7s" value="0dB"/></keyframeAnimation></param></adjust-volume></asset-clip>
<asset-clip name="Duplicate" ref="r2" offset="6s" start="0s" duration="2s"/>
</spine></sequence></project></event></library></fcpxml>'''
CLIP = '/fcpxml[1]/library[1]/event[1]/project[1]/sequence[1]/spine[1]/asset-clip[1]'
POINTS = [{"time": "0s", "speed": 1}, {"time": "2s", "speed": 2},
          {"time": "4s", "speed": 1}]


@pytest.fixture
def project(tmp_path, monkeypatch):
    p = tmp_path / 'input.fcpxml'
    p.write_text(XML)
    monkeypatch.setattr(server, 'READ_ROOTS', [str(tmp_path)])
    monkeypatch.delenv('FCP_MCP_AUTOPUSH', raising=False)
    return p


async def call(action, path, **kw):
    group = 'inspect' if action == 'list_speed_points' else 'edit'
    result = await server.call_tool(group, {'action': action, 'args': {'filepath': str(path), **kw}})
    return result[0].text


async def write(path, **kw):
    return await call('set_speed_curve', path, clip_path=CLIP,
                      **{'speed_keyframes': POINTS, **kw})


def test_speed_schemas_are_discoverable_on_both_surfaces():
    flat = {t.name: tool_input_schema(t) for t in server._legacy_tool_list()}
    for name, schema in rt.ACTION_SCHEMAS.items():
        assert flat[name] == schema
        assert name in server.TOOL_HANDLERS
        assert 'filepath' in server._action_param_help(name)
    assert 'list_speed_points' in server.TOOL_GROUPS['inspect']['actions']
    grouped = tool_input_schema(server._group_tool('edit'))
    names = {c['if']['properties']['action']['const'] for c in grouped['allOf']}
    assert {'set_speed_curve', 'reset_speed', 'set_keyframes'} <= names
    point = flat['set_speed_curve']['properties']['speed_keyframes']['items']
    assert point['oneOf'] == [{'required': ['time']}, {'required': ['frame']}]


@pytest.mark.asyncio
async def test_read_only_listing_does_not_publish(project):
    before = project.read_bytes()
    data = json.loads(await call('list_speed_points', project))
    assert len(data['clips']) == 2
    assert data['clips'][0]['clip_path'] == CLIP
    assert project.read_bytes() == before
    assert journal.records(str(project)) == []


@pytest.mark.asyncio
async def test_real_grouped_ramp_preserves_source_animation_and_ripples(project):
    before = project.read_bytes()
    result = json.loads(await write(project))
    out = Path(result['output_path'])
    assert out.name == 'input_retimed.fcpxml'
    assert project.read_bytes() == before
    root = ET.parse(out)
    a, b = root.findall('.//spine/asset-clip')
    assert a.get('duration') == '4s' and b.get('offset') == '4s'
    assert root.find('.//sequence').get('duration') == '6s'
    points = a.findall('timeMap/timept')
    assert len(points) == 121
    assert points[0].get('time') == '5s' and points[0].get('value') == '5s'
    assert points[-1].get('time') == '9s' and points[-1].get('value') == '11s'
    assert [(p.get('time'), p.get('value')) for p in a.findall('.//keyframe')] == [('5s', '-24dB'), ('7s', '0dB')]
    assert a.find('timeMap').get('preservesPitch') == '1'
    assert 'unverified' in result['verification']
    rows = journal.records(str(project))
    assert len(rows) == 1 and rows[0]['action'] == 'set_speed_curve'
    assert rows[0]['output']['path'] == str(out)


@pytest.mark.asyncio
async def test_flat_call_reset_and_keyframes_after_retime(project):
    out = json.loads(await write(project))['output_path']
    # After retiming intrinsic API still uses adjusted local time, not media time.
    response = await server.call_tool('set_keyframes', {
        'filepath': out, 'clip_path': CLIP, 'property': 'rotation',
        'keyframes': [{'time': '2s', 'value': 15}],
    })
    animated = json.loads(response[0].text)['output_path']
    assert ET.parse(animated).find('.//adjust-transform//keyframe').get('time') == '7s'
    response = await server.call_tool('reset_speed', {'filepath': animated, 'clip_path': CLIP})
    reset = json.loads(response[0].text)['output_path']
    clip = ET.parse(reset).find('.//spine/asset-clip')
    assert clip.find('timeMap') is None and clip.get('duration') == '6s'
    assert clip.find('.//adjust-transform//keyframe').get('time') == '7s'


@pytest.mark.asyncio
@pytest.mark.parametrize('changes', [
    {'speed_keyframes': []}, {'speed_keyframes': [{'time': '0s', 'speed': True}, {'time': '4s', 'speed': 1}]},
    {'speed_keyframes': [{'time': '0s', 'speed': float('nan')}, {'time': '4s', 'speed': 1}]},
    {'speed_keyframes': [{'time': '0s', 'speed': 0}, {'time': '4s', 'speed': 1}]},
    {'speed_keyframes': [{'time': '0s', 'frame': 0, 'speed': 1}, {'time': '4s', 'speed': 1}]},
    {'speed_keyframes': [{'time': '0s', 'speed': 1}, {'time': '4s', 'speed': 1, 'unknown': 1}]},
    {'preserve_pitch': 'yes'}, {'ripple': 1}, {'frame_sampling': 'made-up'},
    {'ripple': False}, {'unknown': True},
])
async def test_invalid_requests_publish_nothing(project, changes):
    before = project.read_bytes()
    response = await write(project, **changes)
    assert 'Error' in response or 'error' in response
    assert project.read_bytes() == before
    assert list(project.parent.glob('*retimed*')) == []
    assert journal.records(str(project)) == []


@pytest.mark.asyncio
async def test_failed_dtd_does_not_publish_or_journal(project, monkeypatch):
    monkeypatch.setattr(kt.dtd, 'validate_against_dtd', lambda _: (False, 'sentinel invalid'))
    response = await write(project)
    assert 'sentinel invalid' in response
    assert not list(project.parent.glob('*retimed*'))
    assert journal.records(str(project)) == []


@pytest.mark.asyncio
async def test_existing_output_is_preserved_and_defaults_number(project):
    first = json.loads(await write(project))['output_path']
    before = Path(first).read_bytes()
    assert 'error' in (await write(project, output_path=first)).lower()
    second = json.loads(await write(project))['output_path']
    assert Path(second).name == 'input_retimed_2.fcpxml'
    assert Path(first).read_bytes() == before
    assert 'error' in (await write(project, output_path=str(project))).lower()


@pytest.mark.asyncio
async def test_bundle_preserves_sidecars_and_pitch_option(project):
    bundle = project.parent / 'input.fcpxmld'
    bundle.mkdir()
    (bundle / 'Info.fcpxml').write_text(XML)
    (bundle / 'sidecar.bin').write_bytes(b'original sidecar')
    out = Path(json.loads(await write(bundle, preserve_pitch=False))['output_path'])
    assert out.is_dir() and out.suffix == '.fcpxmld'
    assert (out / 'sidecar.bin').read_bytes() == b'original sidecar'
    assert ET.parse(out / 'Info.fcpxml').find('.//timeMap').get('preservesPitch') == '0'
