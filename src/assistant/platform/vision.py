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
    usable: list[str] = []
    results: dict[int, str] = {}
    index_of: dict[str, int] = {}
    for i, p in enumerate(paths):
        path = Path(p)
        if not path.is_file():
            results[i] = f"[image unavailable: {path.name} not found]"
        elif media_type_for(path) is None:
            results[i] = f"[unsupported image type: {path.name}]"
        elif path.stat().st_size > _MAX_IMAGE_BYTES:
            results[i] = f"[image too large to process: {path.name}]"
        else:
            usable.append(str(path))
            index_of[str(path)] = i

    if usable:
        described = None
        for backend in (_remote_describe,):
            try:
                described = backend(settings, usable)
                if described is not None:
                    break
            except Exception as exc:
                log.warning("vision backend %s failed: %s", backend.__name__, exc)
        if described is None:
            described = ["[image could not be analyzed: no vision backend "
                         "available — see VISION_* in .env]"] * len(usable)
        for path, text in zip(usable, described):
            results[index_of[path]] = text
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
