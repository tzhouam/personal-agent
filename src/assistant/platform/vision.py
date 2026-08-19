"""Image understanding: turn attached images into text the chat agent can use.

With a natively multimodal main LLM (`LLM_SUPPORTS_IMAGES=true`) images are
attached directly to the model call and this module is bypassed. For
text-only main models, images follow a describe-then-reason path: a remote
multimodal API (`VISION_API_KEY`/`VISION_MODEL`, Anthropic- or OpenAI-style
wire format via `VISION_PROVIDER`) produces one detailed description per
image (scene + verbatim text transcription) and the description is injected
into the chat prompt as read-only context. There is deliberately NO local
model path (owner decision 2026-07-12).

`describe_images` degrades, never raises: an unusable image or an
unconfigured/failing API yields a bracketed error string the model can
acknowledge honestly.
"""

import base64
import logging
from pathlib import Path

from assistant.platform.config import Settings

log = logging.getLogger("assistant")

_DESCRIBE_PROMPT = (
    "Describe this image in detail for someone who cannot see it. "
    "Cover the scene, objects, people, layout, and any notable details. "
    "Transcribe ALL visible text verbatim in its original language "
    "(labels, signs, UI text, code, handwriting). If it is a screenshot, "
    "chart, or document, explain its structure and content precisely."
)

_MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".webp": "image/webp"}
_MAX_IMAGE_BYTES = 10 * 1024 * 1024


def media_type_for(path: str | Path) -> str | None:
    """Anthropic media type for an image file extension, or None if it isn't
    an image type the vision chain accepts."""
    return _MEDIA_TYPES.get(Path(path).suffix.lower())


def describe_images(settings: Settings, paths: list[str]) -> list[str]:
    """One description per input path, in order — the only entry point.

    Unreadable/oversized/non-image paths get a bracketed error string instead
    of a description, and backend failures fall through the chain; the caller
    always gets `len(paths)` strings back."""
    usable: list[str] = []          # unique paths, first-occurrence order
    results: dict[int, str] = {}
    indices: dict[str, list[int]] = {}   # path → every input position holding it
    for i, p in enumerate(paths):
        path = Path(p)
        try:  # a file deleted between is_file() and stat() must degrade to an
            # error string, not break the "never raises" contract
            missing = not path.is_file()
            too_big = not missing and path.stat().st_size > _MAX_IMAGE_BYTES
        except OSError:
            missing, too_big = True, False
        if missing:
            results[i] = f"[image unavailable: {path.name} not found]"
        elif media_type_for(path) is None:
            results[i] = f"[unsupported image type: {path.name}]"
        elif too_big:
            results[i] = f"[image too large to process: {path.name}]"
        else:
            # The same file can appear twice in one message (an email with an
            # image both inline and attached stages to one sha1-named path) —
            # describe it once, fan the description out to every position. A
            # plain path→index map dropped the first occurrence and broke the
            # len(paths) contract with a KeyError, which upstream turned into
            # a silently dropped owner email.
            key = str(path)
            if key not in indices:
                indices[key] = []
                usable.append(key)
            indices[key].append(i)

    if usable:
        described = None
        configured = bool(settings.vision_api_key and settings.vision_model)
        for backend in (_remote_describe,):
            try:
                described = backend(settings, usable)
                if described is not None:
                    break
            except Exception as exc:
                log.warning("vision backend %s failed: %s", backend.__name__, exc)
        if not described:  # None (no backend) and [] (empty return) both fail
            # A configured backend that ERRORED is a transient failure — do
            # not tell the owner vision isn't set up (that message sent
            # owners on pointless reconfiguration hunts).
            described = [("[image analysis failed this time (backend error) "
                          "— try again]") if configured else
                         ("[image could not be analyzed: no vision backend "
                          "available — see VISION_* in .env]")] * len(usable)
        # Cardinality guard: the contract holds even against a backend that
        # miscounts — short returns are padded, long ones trimmed.
        if len(described) < len(usable):
            log.warning("vision backend returned %d descriptions for %d images",
                        len(described), len(usable))
            described += ["[image could not be analyzed: backend returned "
                          "no result]"] * (len(usable) - len(described))
        elif len(described) > len(usable):
            log.warning("vision backend returned %d descriptions for %d images "
                        "— extra discarded", len(described), len(usable))
            described = described[:len(usable)]
        for path, text in zip(usable, described):
            for i in indices[path]:
                results[i] = text
    return [results[i] for i in range(len(paths))]


def _remote_describe(settings: Settings, paths: list[str]) -> list[str] | None:
    """Describe via the configured multimodal API. `VISION_PROVIDER` picks
    the wire format: "anthropic" (default — real Anthropic or compatible) or
    "openai" (OpenAI, Gemini's openai-compatible endpoint, DashScope/Qwen-VL,
    …). None when unconfigured."""
    if not (settings.vision_api_key and settings.vision_model):
        return None
    if settings.vision_provider.strip().lower() == "openai":
        return _openai_describe(settings, paths)
    import anthropic

    kwargs: dict = {"api_key": settings.vision_api_key}
    if settings.vision_base_url:
        kwargs["base_url"] = settings.vision_base_url
    client = anthropic.Anthropic(**kwargs)
    out = []
    for path in paths:
        data = base64.b64encode(Path(path).read_bytes()).decode()
        resp = client.messages.create(
            model=settings.vision_model, max_tokens=1000,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64",
                                             "media_type": media_type_for(path),
                                             "data": data}},
                {"type": "text", "text": _DESCRIBE_PROMPT},
            ]}])
        out.append("".join(b.text for b in resp.content if b.type == "text").strip())
    return out


def _openai_describe(settings: Settings, paths: list[str]) -> list[str]:
    """OpenAI-style `chat/completions` with data-URI image_url content —
    plain httpx, no SDK dependency."""
    import httpx

    base = (settings.vision_base_url or "https://api.openai.com/v1").rstrip("/")
    out = []
    for path in paths:
        data = base64.b64encode(Path(path).read_bytes()).decode()
        uri = f"data:{media_type_for(path)};base64,{data}"
        resp = httpx.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {settings.vision_api_key}"},
            json={"model": settings.vision_model, "max_tokens": 1000,
                  "messages": [{"role": "user", "content": [
                      {"type": "image_url", "image_url": {"url": uri}},
                      {"type": "text", "text": _DESCRIBE_PROMPT},
                  ]}]},
            timeout=120)
        resp.raise_for_status()
        out.append(str(resp.json()["choices"][0]["message"]["content"] or "").strip())
    return out


def render_image_context(descriptions: list[str]) -> str:
    """The prompt block the chat agent appends when a message has images."""
    lines = [f"[image {i + 1}] {d}" for i, d in enumerate(descriptions)]
    return ("## Attached images (described by a vision model — treat as what "
            "the owner is showing you)\n" + "\n".join(lines))
