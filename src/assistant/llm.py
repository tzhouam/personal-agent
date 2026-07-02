import json
import re

import anthropic
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import Settings

_RETRYABLE = (
    anthropic.APIConnectionError,
    anthropic.RateLimitError,
    anthropic.InternalServerError,
)


class LLM:
    def __init__(self, settings: Settings):
        kwargs: dict = {"api_key": settings.anthropic_api_key}
        if settings.anthropic_base_url:
            kwargs["base_url"] = settings.anthropic_base_url
        self.client = anthropic.Anthropic(**kwargs)
        self.default_model = settings.anthropic_model
        self.cheap_model = settings.cheap_model

    @retry(
        retry=retry_if_exception_type(_RETRYABLE),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, max=30),
        reraise=True,
    )
    def complete(
        self,
        prompt: str,
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 4000,
    ) -> str:
        kwargs: dict = {
            "model": model or self.default_model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system
        resp = self.client.messages.create(**kwargs)
        if resp.stop_reason == "max_tokens":
            import logging

            logging.getLogger("assistant").warning(
                "LLM response truncated at max_tokens=%s — raise the budget for this call",
                kwargs["max_tokens"],
            )
        return "".join(b.text for b in resp.content if b.type == "text")

    def complete_json(self, prompt: str, system: str | None = None, **kw):
        """One retry with error feedback if the first response isn't valid JSON."""
        text = self.complete(prompt, system=system, **kw)
        try:
            return _parse_json(text)
        except ValueError as exc:
            retry_prompt = (
                f"{prompt}\n\nYour previous response could not be parsed as JSON "
                f"({exc}). Respond again with ONLY valid JSON, no prose, no code fences."
            )
            return _parse_json(self.complete(retry_prompt, system=system, **kw))


def _parse_json(text: str):
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    if start < 0:
        raise ValueError("no JSON object or array found in response")
    for end in range(len(text), start, -1):
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            continue
    raise ValueError("unparseable JSON in response")
