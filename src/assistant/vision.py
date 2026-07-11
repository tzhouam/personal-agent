"""Image understanding: turn attached images into text the chat agent can use.

The main LLM (an Anthropic-compatible endpoint, e.g. DeepSeek) is text-only,
so images follow a describe-then-reason path: a vision backend produces one
detailed description per image (scene + verbatim text transcription) and the
description is injected into the chat prompt as read-only context. Backends
form a fallback chain, same idiom as `search.py`:

1. **Remote** — any Anthropic-compatible vision endpoint, when
   `VISION_API_KEY`/`VISION_MODEL` (+ optional `VISION_BASE_URL`) are set.
2. **Local** — Qwen3-VL served one-shot from a subprocess
   (`vision_worker.py`) on the freest CUDA device, when
   `VISION_LOCAL_MODEL_PATH` points at downloaded weights. The subprocess
   loads, describes, and exits, so no GPU memory is held between images —
   the GPUs are shared with CI jobs.

`describe_images` degrades, never raises: an unusable image or a dead chain
yields a bracketed error string the model can acknowledge honestly.
"""

import base64
import json
import logging
import subprocess
import sys
from pathlib import Path

from .config import Settings

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
_WORKER_TIMEOUT_S = 300


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
        for backend in (_remote_describe, _local_describe):
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
    """Describe via the configured Anthropic-compatible vision endpoint; None
    when unconfigured (falls through to the local backend)."""
    if not (settings.vision_api_key and settings.vision_model):
        return None
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


def _local_describe(settings: Settings, paths: list[str]) -> list[str] | None:
    """Describe via the local VLM in a one-shot subprocess; None when no local
    model is configured or its weights aren't on disk."""
    model_path = Path(settings.vision_local_model_path or "")
    if not settings.vision_local_model_path or not model_path.is_dir():
        return None
    payload = json.dumps({"model_path": str(model_path), "images": paths,
                          "prompt": _DESCRIBE_PROMPT,
                          "min_free_gib": settings.vision_min_free_gib})
    proc = subprocess.run(
        [sys.executable, "-m", "assistant.vision_worker"],
        input=payload, capture_output=True, text=True, timeout=_WORKER_TIMEOUT_S)
    if proc.returncode != 0:
        raise RuntimeError(
            f"vision worker rc={proc.returncode}: {proc.stderr.strip()[-300:]}")
    return json.loads(proc.stdout)["descriptions"]


def render_image_context(descriptions: list[str]) -> str:
    """The prompt block the chat agent appends when a message has images."""
    lines = [f"[image {i + 1}] {d}" for i, d in enumerate(descriptions)]
    return ("## Attached images (described by a vision model — treat as what "
            "the owner is showing you)\n" + "\n".join(lines))
