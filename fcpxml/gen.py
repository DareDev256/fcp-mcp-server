"""Generative fill — quote, then confirm, then insert.

Generated video costs real money per second and the model asking for it is
not the one paying. So the order is fixed: ``quote`` prices the job from a
dated table and returns an id; generation REFUSES without ``confirm=true``
and a quote id that still matches the job; and nothing is attempted above a
per-call cap (``FCP_MCP_GEN_MAX_USD``, default 5). The media that comes
back is probed with ffprobe for its real duration and frame rate — the
model never invents an fps — then attached as a connected clip with the
``generated`` role and a marker naming prompt and provider, so the edit says
where its pictures came from.

Keys come from the environment only and travel in one place: the
provider's auth header. ``_http`` refuses to send a request whose URL or
body carries a key, and every string a handler returns passes through
:func:`redact`. Both guards are mutation-checked in ``tests/test_gen.py``.

Prices are THIRD-PARTY AND UNVERIFIED. They are what the providers' public
pages and resellers reported on ``PRICES_AS_OF``; a quote is an estimate,
and the output says so.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Optional
from xml.etree import ElementTree as ET

from .models import MarkerType
from .rational import format_seconds, parse_seconds
from .writer import (
    SPINE_ELEMENT_TAGS,
    FCPXMLModifier,
    _create_asset_element,
    _dtd_insert,
    _sanitize_xml_value,
    build_marker_element,
)

# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------

# USD per generated second, the UPPER end of every range seen so a quote
# never under-promises. Sources, all read 2026-10-09 and none verified
# against an invoice: Google Gemini API pricing page (Veo 3.1 ~$0.10-0.40/s
# by tier and audio), fal.ai model pages (Kling 2.5 ~$0.04-0.07/s, Wan 2.5
# ~$0.05-0.15/s), Runway's credit table (Aleph 15 credits/s at $0.01/credit),
# Luma's Ray Modify page (no per-second figure published). Update the date
# when the numbers are.
PRICES_AS_OF = "2026-10-09"

PRICES: dict[tuple[str, str], dict[str, Any]] = {
    ("fal", "kling-2.5"): {
        "usd_per_second": Decimal("0.07"),
        "billable": (5, 10),
        "endpoint": "fal-ai/kling-video/v2.5-turbo/pro/image-to-video",
        "text_endpoint": "fal-ai/kling-video/v2.5-turbo/pro/text-to-video",
    },
    ("fal", "wan-2.5"): {
        "usd_per_second": Decimal("0.15"),
        "billable": (5, 10),
        "endpoint": "fal-ai/wan-25-preview/image-to-video",
        "text_endpoint": "fal-ai/wan-25-preview/text-to-video",
    },
    ("gemini", "veo-3.1"): {
        "usd_per_second": Decimal("0.40"),
        "billable": (4, 6, 8),
        "endpoint": "models/veo-3.1-generate-preview:predictLongRunning",
    },
    ("runway", "aleph"): {
        "usd_per_second": Decimal("0.15"),
        "billable": (),
        "video_to_video": True,
    },
    ("luma", "ray-modify"): {
        "usd_per_second": None,
        "billable": (),
        "video_to_video": True,
    },
}

DEFAULT_MODEL = {"fal": "kling-2.5", "gemini": "veo-3.1", "runway": "aleph", "luma": "ray-modify"}
KEY_ENV = {"fal": "FAL_KEY", "gemini": "GEMINI_API_KEY", "runway": "RUNWAY_API_KEY", "luma": "LUMA_API_KEY"}
MAX_USD_ENV = "FCP_MCP_GEN_MAX_USD"
DEFAULT_MAX_USD = Decimal("5")
QUOTE_TTL_SECONDS = 3600
MAX_SECONDS = 120
GENERATED_ROLE = "generated"
HTTP_TIMEOUT = 120
POLL_INTERVAL = 3.0
POLL_LIMIT = 200


class GenError(Exception):
    """Base: anything this module refuses or cannot do. The message is the answer."""


class RefusedError(GenError):
    """A guard said no: no confirm, no quote, over cap, no key, not implemented."""


class ProviderError(GenError):
    """The provider failed. Already redacted by the time it reaches a handler."""


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------

def cap_usd() -> Decimal:
    """The per-call ceiling. A malformed env value raises rather than defaulting:
    a cap that silently became 5 because someone typed `$4` is no cap."""
    raw = os.environ.get(MAX_USD_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_USD
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"{MAX_USD_ENV} must be a decimal number of USD, got {raw!r}") from None
    if value < 0:
        raise ValueError(f"{MAX_USD_ENV} must not be negative, got {raw!r}")
    return value


def billable_seconds(provider: str, model: str, seconds: Fraction) -> int:
    """What the provider charges for *seconds*: the next supported clip length.

    Kling and Wan bill 5 or 10; Veo bills 4, 6 or 8. A 7-second gap on Kling
    is a 10-second generation and the quote must say 10, not 7.
    """
    steps = PRICES[(provider, model)]["billable"]
    need = math.ceil(seconds)
    for step in steps:
        if need <= step:
            return step
    if steps:
        raise RefusedError(
            f"{provider}/{model} generates at most {max(steps)}s per call; "
            f"{float(seconds):.2f}s was asked for. Split the job."
        )
    return max(1, need)


@dataclass(frozen=True)
class Quote:
    id: str
    provider: str
    model: str
    requested_seconds: Fraction
    billable_seconds: int
    usd: Optional[Decimal]
    cap_usd: Decimal
    prices_as_of: str
    available: bool
    created: float

    @property
    def over_cap(self) -> bool:
        return self.usd is not None and self.usd > self.cap_usd

    def as_dict(self) -> dict:
        return {
            "quote_id": self.id,
            "provider": self.provider,
            "model": self.model,
            "requested_seconds": f"{self.requested_seconds.numerator}/{self.requested_seconds.denominator}",
            "billable_seconds": self.billable_seconds,
            "estimated_usd": None if self.usd is None else str(self.usd),
            "cap_usd": str(self.cap_usd),
            "over_cap": self.over_cap,
            "prices_as_of": self.prices_as_of,
            "price_verified": False,
            "available": self.available,
        }


_QUOTES: dict[str, Quote] = {}


def _quote_id(provider: str, model: str, billable: int, prompt: str, usd: Optional[Decimal]) -> str:
    raw = f"{provider}|{model}|{billable}|{prompt}|{usd}|{PRICES_AS_OF}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def resolve_model(provider: str, model: Optional[str]) -> str:
    if provider not in DEFAULT_MODEL:
        raise RefusedError(f"Unknown provider {provider!r}. Providers: {', '.join(DEFAULT_MODEL)}.")
    model = model or DEFAULT_MODEL[provider]
    if (provider, model) not in PRICES:
        known = ", ".join(m for p, m in PRICES if p == provider)
        raise RefusedError(f"Unknown model {model!r} for {provider}. Models: {known}.")
    return model


def quote(provider: str, seconds: Any, prompt: str, model: Optional[str] = None) -> Quote:
    """Price a job and register the quote for :func:`confirm_quote`."""
    q = _build_quote(provider, seconds, prompt, model)
    _QUOTES[q.id] = q
    return q


def _build_quote(provider: str, seconds: Any, prompt: str, model: Optional[str] = None) -> Quote:
    """Price a job. Pure: the gate recomputes with this and must not re-register,
    or an expired quote would be refreshed by the very call checking it."""
    model = resolve_model(provider, model)
    try:
        requested = Fraction(str(seconds)).limit_denominator(100000)
    except (ValueError, ZeroDivisionError):
        raise RefusedError(f"seconds must be a number, got {seconds!r}") from None
    if not 0 < requested <= MAX_SECONDS:
        raise RefusedError(f"seconds must be between 0 and {MAX_SECONDS}, got {float(requested):.2f}")
    if not prompt or not prompt.strip():
        raise RefusedError("A prompt is required; the quote is bound to it.")
    entry = PRICES[(provider, model)]
    billable = billable_seconds(provider, model, requested)
    per_second = entry["usd_per_second"]
    usd = None if per_second is None else (per_second * billable).quantize(Decimal("0.01"))
    return Quote(
        id=_quote_id(provider, model, billable, prompt, usd),
        provider=provider, model=model, requested_seconds=requested,
        billable_seconds=billable, usd=usd, cap_usd=cap_usd(),
        prices_as_of=PRICES_AS_OF,
        available=not entry.get("video_to_video", False),
        created=time.time(),
    )


def confirm_quote(
    quote_id: Optional[str], confirm: Any, provider: str, seconds: Any, prompt: str,
    model: Optional[str] = None,
) -> Quote:
    """The money gate. Returns the quote to spend against, or refuses.

    Four refusals, each naming the fix: no ``confirm=true``; no quote id or
    an unknown/expired one; a quote whose job no longer matches (prompt,
    seconds, provider or model changed, so the price changed); a price over
    the cap. The quote is recomputed from the job rather than trusted from
    the register, so a stale id cannot buy a different job.
    """
    model = resolve_model(provider, model)
    fresh = _build_quote(provider, seconds, prompt, model)
    how = json.dumps({"action": "gen_quote", "args": {
        "provider": provider, "model": model, "seconds": str(seconds), "prompt": prompt}})
    if confirm is not True:
        raise RefusedError(
            f"Refused: generation spends money and needs confirm=true with a quote id. "
            f"Quote first: gen {how} — this job is {fresh.billable_seconds}s on "
            f"{provider}/{model}, about ${fresh.usd} (prices as of {PRICES_AS_OF}, unverified)."
        )
    if not quote_id:
        raise RefusedError(f"Refused: no quote_id. Quote first: gen {how}")
    held = _QUOTES.get(quote_id)
    if held is None or time.time() - held.created > QUOTE_TTL_SECONDS:
        raise RefusedError(
            f"Refused: quote {quote_id} is unknown or older than {QUOTE_TTL_SECONDS // 60} minutes "
            f"(quotes live in this server process). Quote again: gen {how}"
        )
    if held.id != fresh.id:
        raise RefusedError(
            f"Refused: quote {quote_id} was for a different job (provider, model, seconds or "
            f"prompt changed, so the price changed). Quote again: gen {how}"
        )
    if not fresh.available:
        raise RefusedError(
            f"Refused: {provider}/{model} is video-to-video and its API is not implemented "
            "here (docs/ROADMAP-2026-10.md). The quote is an estimate only."
        )
    if fresh.over_cap:
        raise RefusedError(
            f"Refused: ${fresh.usd} is over the per-call cap of ${fresh.cap_usd} "
            f"({MAX_USD_ENV}). Shorten the job or raise the cap deliberately."
        )
    return fresh


# ---------------------------------------------------------------------------
# Keys: one place they may appear
# ---------------------------------------------------------------------------

def present_keys() -> dict[str, str]:
    return {env: os.environ[env] for env in KEY_ENV.values() if os.environ.get(env)}


def key_for(provider: str) -> str:
    env = KEY_ENV[provider]
    value = os.environ.get(env, "").strip()
    if not value:
        raise RefusedError(f"Refused: {env} is not set. Export it and call again; it is sent only in {provider}'s auth header.")
    return value


def redact(text: str) -> str:
    """Replace every present key value in *text* with its env name.

    Applied to everything a gen handler returns — including provider error
    bodies, which echo request headers more often than anyone would like.
    """
    for env, value in present_keys().items():
        if len(value) >= 4:
            text = text.replace(value, f"[REDACTED:{env}]")
    return text


def _assert_key_not_outside_headers(url: str, body: Optional[bytes]) -> None:
    for env, value in present_keys().items():
        if value in url or (body is not None and value.encode("utf-8") in body):
            raise RuntimeError(f"refusing to send {env} anywhere but a header")


def _http(method: str, url: str, headers: dict[str, str], body: Optional[bytes] = None,
          timeout: float = HTTP_TIMEOUT) -> tuple[int, bytes]:
    """The one transport. Tests replace it; production guards it."""
    _assert_key_not_outside_headers(url, body)
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ProviderError(f"{method} {url} failed: {redact(str(exc))}") from None


_sleep: Callable[[float], None] = time.sleep


def _json(status: int, data: bytes, what: str) -> dict:
    if status >= 300:
        raise ProviderError(f"{what} returned HTTP {status}: {redact(data[:400].decode('utf-8', 'replace'))}")
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ProviderError(f"{what} returned a non-JSON body") from None


def _data_uri(path: str) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(Path(path).read_bytes()).decode("ascii")


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

class Provider:
    """One job: prompt + seconds (+ a conditioning image) -> MP4 bytes."""

    name = ""

    def __init__(self, model: str):
        self.model = model

    def generate(self, prompt: str, seconds: int, *, image_path: Optional[str] = None) -> bytes:
        raise NotImplementedError

    @property
    def label(self) -> str:
        return f"{self.name}/{self.model}"


class FalProvider(Provider):
    """fal.ai queue API: submit, poll, fetch result, download the file.

    The key rides in ``Authorization: Key …`` on queue.fal.run only; the
    result's download URL is a signed CDN link and gets no header.
    """

    name = "fal"
    QUEUE = "https://queue.fal.run/"

    def generate(self, prompt, seconds, *, image_path=None):
        key = key_for("fal")
        entry = PRICES[("fal", self.model)]
        endpoint = entry["endpoint"] if image_path else entry["text_endpoint"]
        payload: dict[str, Any] = {"prompt": prompt, "duration": str(seconds)}
        if image_path:
            payload["image_url"] = _data_uri(image_path)
        auth = {"Authorization": f"Key {key}", "Content-Type": "application/json"}
        status, data = _http("POST", self.QUEUE + endpoint, auth, json.dumps(payload).encode("utf-8"))
        submitted = _json(status, data, "fal submit")
        status_url = submitted.get("status_url") or f"{self.QUEUE}{endpoint}/requests/{submitted.get('request_id')}/status"
        response_url = submitted.get("response_url") or f"{self.QUEUE}{endpoint}/requests/{submitted.get('request_id')}"
        for _ in range(POLL_LIMIT):
            status, data = _http("GET", status_url, {"Authorization": f"Key {key}"})
            state = _json(status, data, "fal status").get("status")
            if state == "COMPLETED":
                break
            if state in ("FAILED", "ERROR"):
                raise ProviderError("fal reported the generation failed")
            _sleep(POLL_INTERVAL)
        else:
            raise ProviderError("fal generation did not finish in time")
        status, data = _http("GET", response_url, {"Authorization": f"Key {key}"})
        result = _json(status, data, "fal result")
        video = (result.get("video") or {}).get("url")
        if not video:
            raise ProviderError("fal result carried no video url")
        status, data = _http("GET", video, {})
        if status >= 300 or not data:
            raise ProviderError(f"fal video download returned HTTP {status}")
        return data


class GeminiProvider(Provider):
    """Veo through the Gemini API: predictLongRunning, poll the operation, download.

    The key rides in ``x-goog-api-key`` on generativelanguage.googleapis.com
    only, including the file download, which that host requires.
    """

    name = "gemini"
    BASE = "https://generativelanguage.googleapis.com/v1beta/"

    def generate(self, prompt, seconds, *, image_path=None):
        key = key_for("gemini")
        auth = {"x-goog-api-key": key, "Content-Type": "application/json"}
        instance: dict[str, Any] = {"prompt": prompt}
        if image_path:
            instance["image"] = {
                "bytesBase64Encoded": base64.b64encode(Path(image_path).read_bytes()).decode("ascii"),
                "mimeType": "image/jpeg",
            }
        body = json.dumps({"instances": [instance], "parameters": {"durationSeconds": seconds}}).encode("utf-8")
        endpoint = PRICES[("gemini", self.model)]["endpoint"]
        status, data = _http("POST", self.BASE + endpoint, auth, body)
        op = _json(status, data, "veo submit").get("name")
        if not op:
            raise ProviderError("veo submit returned no operation name")
        for _ in range(POLL_LIMIT):
            status, data = _http("GET", self.BASE + op, {"x-goog-api-key": key})
            result = _json(status, data, "veo operation")
            if result.get("done"):
                break
            _sleep(POLL_INTERVAL)
        else:
            raise ProviderError("veo generation did not finish in time")
        if result.get("error"):
            raise ProviderError(f"veo failed: {redact(str(result['error']))[:300]}")
        samples = ((result.get("response") or {}).get("generateVideoResponse") or {}).get("generatedSamples") or []
        uri = ((samples[0] if samples else {}).get("video") or {}).get("uri")
        if not uri:
            raise ProviderError("veo operation finished with no video")
        if not uri.startswith(self.BASE) and "googleapis.com" not in uri:
            raise ProviderError("veo returned a download uri off googleapis.com; refusing to send the key there")
        status, data = _http("GET", uri, {"x-goog-api-key": key})
        if status >= 300 or not data:
            raise ProviderError(f"veo video download returned HTTP {status}")
        return data


class NotImplementedProvider(Provider):
    """Runway Aleph and Luma Ray Modify are video-to-video. Honest stub."""

    def __init__(self, name: str, model: str):
        super().__init__(model)
        self.name = name

    def generate(self, prompt, seconds, *, image_path=None):
        raise RefusedError(
            f"{self.label} is video-to-video and its API is not implemented here "
            "(docs/ROADMAP-2026-10.md)."
        )


def provider_for(provider: str, model: Optional[str] = None) -> Provider:
    model = resolve_model(provider, model)
    if provider == "fal":
        return FalProvider(model)
    if provider == "gemini":
        return GeminiProvider(model)
    return NotImplementedProvider(provider, model)


# ---------------------------------------------------------------------------
# What came back
# ---------------------------------------------------------------------------

def probe_media(path: str) -> Optional[dict]:
    """Real duration, frame rate and size from ffprobe, as Fractions.

    None when ffprobe is absent or the file is unreadable. The caller must
    then refuse to insert: an fps the probe did not measure is an fps the
    model made up, and Final Cut would conform the clip to a lie.
    """
    if shutil.which("ffprobe") is None or not Path(path).is_file():
        return None
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
             "-show_format", str(path)],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        info = json.loads(proc.stdout or "{}")
    except ValueError:
        return None
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        return None
    try:
        fps = Fraction(video.get("r_frame_rate") or video.get("avg_frame_rate") or "")
        duration = Fraction(str((info.get("format") or {}).get("duration") or video.get("duration") or "")).limit_denominator(100000)
    except (ValueError, ZeroDivisionError):
        return None
    if fps <= 0 or duration <= 0:
        return None
    return {
        "fps": fps,
        "duration": duration,
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "has_audio": any(s.get("codec_type") == "audio" for s in info.get("streams", [])),
    }


def _fps_label(fps: Fraction) -> str:
    return str(fps.numerator // fps.denominator) if fps.denominator == 1 else f"{float(fps):.2f}".rstrip("0").rstrip(".")


def _find_host(
    modifier: FCPXMLModifier, absolute: Fraction, needed: Fraction, frame: Fraction
) -> tuple[ET.Element, bool]:
    """The spine element whose span covers *absolute* seconds, gaps included.

    A sequence can be longer than its spine: ``examples/sample.fcpxml`` runs
    120s with 53.75s of picture, and the tail has no element at all. A
    connected clip has to hang off something, so when the position is past
    the last spine element a ``<gap>`` is appended to reach it — which is
    exactly what Final Cut writes for an empty stretch of the storyline.
    Returns ``(host, created)``.
    """
    spine = modifier._get_spine()
    end = Fraction(0)
    for child in spine:
        if child.tag not in SPINE_ELEMENT_TAGS or child.tag == "transition":
            continue
        offset = parse_seconds(child.get("offset", "0s"))
        duration = parse_seconds(child.get("duration", "0s"))
        if offset <= absolute < offset + duration:
            return child, False
        end = max(end, offset + duration)
    if absolute < end:
        raise ValueError(
            f"No spine element covers {float(absolute):.3f}s (the storyline has a hole there "
            "that is not a <gap>); cannot attach the generated clip."
        )
    span = absolute + needed - end
    gap = ET.SubElement(spine, "gap")
    gap.set("name", "Gap")
    gap.set("offset", format_seconds(end, frame))
    gap.set("start", "0s")
    gap.set("duration", format_seconds(span, frame))
    return gap, True


def _ensure_format(resources: ET.Element, probe: dict) -> str:
    """A <format> matching the probed media, reusing one that already matches."""
    frame = f"{(1 / probe['fps']).numerator}/{(1 / probe['fps']).denominator}s"
    for fmt in resources.findall("format"):
        if (fmt.get("frameDuration") == frame and str(fmt.get("width")) == str(probe["width"])
                and str(fmt.get("height")) == str(probe["height"])):
            return fmt.get("id")
    fmt_id = FCPXMLModifier._unique_resource_id(resources, "r_genfmt1")
    fmt = ET.SubElement(resources, "format")
    fmt.set("id", fmt_id)
    fmt.set("name", f"FFVideoFormat{probe['height']}p{_fps_label(probe['fps'])}")
    fmt.set("frameDuration", frame)
    fmt.set("width", str(probe["width"]))
    fmt.set("height", str(probe["height"]))
    return fmt_id


def insert_generated(
    modifier: FCPXMLModifier,
    *,
    media_path: str,
    probe: dict,
    at: Fraction,
    duration: Fraction,
    name: str,
    prompt: str,
    provider_label: str,
    lane: int = 1,
) -> dict:
    """Attach the generated media at *at* seconds (sequence clock) as a lane clip.

    The clip is snapped to the sequence's frame grid and never claims more
    media than the probe measured. The asset's format comes from the probe,
    so a 30 fps file in a 23.98 sequence is declared as 30 fps and Final Cut
    conforms it, rather than being mislabelled and played fast.
    """
    if lane == 0:
        raise ValueError("lane must be non-zero: a generated clip hangs off the spine, it does not replace it")
    frame = modifier._sequence_frame_duration()
    snapped_at = (at // frame) * frame
    frames = int(min(duration, probe["duration"]) // frame)
    if frames < 1:
        raise ValueError(
            f"Nothing to insert: {float(min(duration, probe['duration'])):.3f}s is under one frame "
            f"of the sequence ({frame})."
        )
    clip_duration = frames * frame
    conformed = clip_duration != duration

    resources = modifier.root.find(".//resources")
    if resources is None:
        raise ValueError("No <resources> element found in FCPXML")
    fmt_id = _ensure_format(resources, probe)
    asset_id = FCPXMLModifier._unique_resource_id(resources, "r_gen1")
    media_frame = 1 / probe["fps"]
    asset = _create_asset_element(
        resources, asset_id, name, Path(media_path).resolve().as_uri(),
        # In the MEDIA's timebase: a 30 fps file's length is a count of its
        # own frames, whatever the sequence runs at.
        duration=format_seconds(probe["duration"], media_frame),
        has_video="1", has_audio="1" if probe["has_audio"] else "0",
    )
    asset.set("format", fmt_id)

    absolute = modifier.timeline_origin() + snapped_at
    host, gap_added = _find_host(modifier, absolute, clip_duration, frame)
    offset_attr = modifier.connected_offset_attribute(absolute, host)
    # Built directly so every time attribute lands in the sequence's own
    # timebase (format_seconds), which is what Final Cut writes and what the
    # writer's validator checks for.
    clip = ET.Element("asset-clip")
    clip.set("ref", asset_id)
    clip.set("offset", format_seconds(offset_attr, frame))
    clip.set("name", _sanitize_xml_value(name, 512))
    clip.set("start", "0s")
    clip.set("duration", format_seconds(clip_duration, frame))
    clip.set("lane", str(lane))
    clip.set("videoRole", GENERATED_ROLE)
    clip.set("format", fmt_id)
    _dtd_insert(host, clip)
    marker_name = _sanitize_xml_value(f"generated: {provider_label} — {prompt}", 256)
    build_marker_element(clip, MarkerType.STANDARD, "0s", format_seconds(frame, frame), marker_name)
    return {
        "asset_id": asset_id, "format_id": fmt_id, "lane": lane,
        "at": snapped_at, "duration": clip_duration, "conformed": conformed,
        "host": host.tag, "gap_added": gap_added, "marker": marker_name,
    }
