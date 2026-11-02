#!/usr/bin/env bash
# Run the suite against ffmpeg 6.1, the build CI gets from Ubuntu's apt.
# Local ffmpeg 9.x hides 6.1-only behaviour (0.28.0: a chained overlay with a
# future-starting stream drew on 9.x and was silently dropped on 6.1).
# Install once, keg-only so the default ffmpeg is untouched: brew install ffmpeg@6
set -euo pipefail
FF6="$(brew --prefix ffmpeg@6 2>/dev/null)/bin"
if [[ ! -x "$FF6/ffmpeg" ]]; then
  echo "ffmpeg@6 is not installed: brew install ffmpeg@6" >&2
  exit 2
fi
export PATH="$FF6:$PATH"
echo "using $(ffmpeg -hide_banner -version | head -1)" >&2
exec uv run --extra dev pytest tests/ -q "$@"
