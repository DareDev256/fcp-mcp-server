#!/bin/bash
# Four-act session for the reveal. Every stage runs the real MCP handlers and
# pushes to Final Cut, so the timeline on the left changes four times on camera.
# Writes /tmp/fcp_stage after each push and pauses, so the operator can open the
# new project into the timeline while the recording is still rolling.
set -uo pipefail
REPO=${REPO:-$(cd "$(dirname "$0")/.." && pwd)}
CLIPS=${CLIPS:?set CLIPS to a directory of source clips}
WORK=/tmp/fcp_session; rm -rf "$WORK"; mkdir -p "$WORK"
TAKES=${TAKES:-$HOME/Movies/fcp-mcp-demo}
STAMP=$(date +%H%M%S)
D=$'\033[2m'; R=$'\033[31m'; C=$'\033[36m'; G=$'\033[32m'; B=$'\033[1m'; N=$'\033[0m'
say () { printf "\n%s❯%s %s%s%s\n" "$C" "$N" "$B" "$1" "$N"; sleep 0.5; }
ok  () { printf "%s   %s%s\n" "$G" "$1" "$N"; }
PAUSE=${PAUSE:-14}
cd "$REPO" || exit 1
PY=./.venv/bin/python

stage () { echo "$1" > /tmp/fcp_stage; sleep "$PAUSE"; }

# ---------- ACT 1 : build a timeline from ten clips ----------
say "cut these ten clips into a Final Cut timeline"
$PY - "$CLIPS" "$WORK" "$STAMP" <<'PY'
import asyncio, json, os, pathlib, sys
sys.path.insert(0, os.environ.get("REPO", os.getcwd())); import server
clips=pathlib.Path(sys.argv[1]); work=pathlib.Path(sys.argv[2]); os.chdir(work)
files=sorted(clips.iterdir())
for p in files: print(f"    {p.name}")
edl={"sources":{p.stem:str(p.resolve()) for p in files},
     "ranges":[{"source":p.stem,"start":0.4,"end":3.4} for p in files]}
pathlib.Path("edl.json").write_text(json.dumps(edl,ensure_ascii=False),encoding="utf-8")
out=asyncio.run(server.call_tool("generate",{"action":"import_edl_json","filepath":"edl.json",
    "output_path":"act1.fcpxml","name":f"Act1 {sys.argv[3]}"}))
print("\n    "+"\n    ".join("\n".join(c.text for c in out).splitlines()[:2]))
PY

push () { # file projectlabel libname
  $PY - "$WORK" "$1" "$3" "$2" <<'PY'
import sys, os
sys.path.insert(0, os.environ.get("REPO", os.getcwd()))
from fcpxml.live import push_to_fcp
work, src, lib, label = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
import re, pathlib
doc = pathlib.Path(f"{work}/{src}")
t = doc.read_text(encoding="utf-8")
t2 = re.sub(r'(<project\s+name=")[^"]*(")', lambda m: m.group(1)+label+m.group(2), t, count=1)
if t2 != t: doc.write_text(t2, encoding="utf-8")
lib_path = os.path.join(os.environ.get("TAKES", os.path.expanduser("~/Movies/fcp-mcp-demo")), f"{lib}.fcpbundle")
r = push_to_fcp(f"{work}/{src}", library_location=lib_path,
                import_copy_path=f"{work}/{src}.import.fcpxml", verify_timeout=90)
print(f"    imported: {', '.join(r['expected_projects'])}")
print(f"    verified: {r['verified']}")
sys.exit(0 if r['verified'] else 1)
PY
}

say "send it to Final Cut"
push act1.fcpxml "Act 1 build" "Act1_$STAMP" || { printf "%s   PUSH FAILED%s\n" "$R" "$N"; exit 1; }
ok "the timeline on the left was not there a minute ago."
stage act1

# ---------- ACT 2 : a marker on every cut ----------
say "put a marker on every cut"
$PY -c "
import asyncio, server
out=asyncio.run(server.call_tool('mark',{'action':'batch_add_markers','filepath':'$WORK/act1.fcpxml','auto_at_cuts':True,'output_path':'$WORK/act2.fcpxml'}))
print('    '+'\n    '.join('\n'.join(c.text for c in out).splitlines()[:3]))
out=asyncio.run(server.call_tool('inspect',{'action':'list_markers','filepath':'$WORK/act2.fcpxml'}))
print('    '+'\n    '.join('\n'.join(c.text for c in out).splitlines()[:9]))
"
say "send that back"
push act2.fcpxml "Act 2 markers" "Act2_$STAMP" || { printf "%s   PUSH FAILED%s\n" "$R" "$N"; exit 1; }
ok "ten markers, placed by reading the cuts."
stage act2

# ---------- ACT 3 : roles ----------
say "tag the roles so the audio can be split out later"
$PY - "$WORK" "$CLIPS" <<'PY'
import asyncio, os, pathlib, sys
sys.path.insert(0, os.environ.get("REPO", os.getcwd())); import server
work=sys.argv[1]; files=sorted(pathlib.Path(sys.argv[2]).iterdir())
src=f"{work}/act2.fcpxml"; dst=f"{work}/act3.fcpxml"
roles=["dialogue","music","effects"]
import shutil; shutil.copy(src,dst)
for i,p in enumerate(files):
    r=roles[i%3]
    out=asyncio.run(server.call_tool("edit",{"action":"assign_role","filepath":dst,
        "clip_id":p.stem,"audio_role":r,"output_path":dst}))
    print(f"    {p.stem[:22]:<24} -> {r}")
out=asyncio.run(server.call_tool("inspect",{"action":"list_roles","filepath":dst}))
print("\n    "+"\n    ".join("\n".join(c.text for c in out).splitlines()[:7]))
PY
say "send that back"
push act3.fcpxml "Act 3 roles" "Act3_$STAMP" || { printf "%s   PUSH FAILED%s\n" "$R" "$N"; exit 1; }
ok "three roles, assigned across ten clips."
stage act3

# ---------- ACT 4 : real audio analysis, dead air removed ----------
say "now find the dead air in this footage and cut it out"
$PY - "$WORK" <<'PY'
import asyncio, json, os, pathlib, subprocess, sys
sys.path.insert(0, os.environ.get("REPO", os.getcwd())); import server
work=pathlib.Path(sys.argv[1]); os.chdir(work)
shots=[]
for i,(col,aud) in enumerate([("cyan","sine=frequency=330"),("orange","anullsrc=r=44100:cl=stereo"),("navy","sine=frequency=440")]):
    out=work/f"take_{i+1}.mp4"
    subprocess.run(["ffmpeg","-y","-v","error","-f","lavfi","-i",f"color=c={col}:s=1280x720:d=6",
        "-f","lavfi","-i",aud,"-shortest","-c:v","libx264","-pix_fmt","yuv420p","-c:a","aac",str(out)],check=True)
    shots.append(out); print(f"    {out.name}")
edl={"sources":{p.stem:str(p) for p in shots},"ranges":[{"source":p.stem,"start":0,"end":6} for p in shots]}
pathlib.Path("edl2.json").write_text(json.dumps(edl),encoding="utf-8")
asyncio.run(server.call_tool("generate",{"action":"import_edl_json","filepath":"edl2.json","output_path":"act4a.fcpxml","name":"Act4"}))
out=asyncio.run(server.call_tool("diagnose",{"action":"detect_media_silence","filepath":"act4a.fcpxml"}))
print("\n    "+"\n    ".join("\n".join(c.text for c in out).splitlines()[:14]))
out=asyncio.run(server.call_tool("edit",{"action":"remove_media_silence","filepath":"act4a.fcpxml","output_path":"act4.fcpxml"}))
print("\n    "+"\n    ".join("\n".join(c.text for c in out).splitlines()[:10]))
PY
say "send the trimmed cut back"
push act4.fcpxml "Act 4 silence" "Act4_$STAMP" || { printf "%s   PUSH FAILED%s\n" "$R" "$N"; exit 1; }
ok "dead air gone. every number above came from the audio itself."
echo done > /tmp/fcp_stage
