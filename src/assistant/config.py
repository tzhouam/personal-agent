"""Central configuration for the assistant.

Exports `Settings`, the single Pydantic-settings object that gathers every
secret, feature toggle, and path from `.env` (repo root, then CWD) and env
vars, plus derived-path/model properties the rest of the package reads."""

import json
import logging
from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[2]  # src/assistant/config.py → repo root


class Settings(BaseSettings):
    """All secrets come from .env (repo root first, then CWD overrides)."""

    model_config = SettingsConfigDict(
        env_file=(_REPO_ROOT / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM — Anthropic SDK, optionally routed to an Anthropic-compatible endpoint
    anthropic_api_key: str = ""
    anthropic_base_url: str = ""
    anthropic_model: str = "claude-sonnet-4-6"
    anthropic_default_haiku_model: str = ""
    # Per-role model routing (JSON in LLM_ROLES). Maps a task role to a model
    # and — since different models often live on different endpoints — an
    # optional base_url + api_key, so different tasks run on different models
    # at once. Roles the code uses: chat, pipeline, research, task, evolve.
    # Anything omitted falls back to the default ANTHROPIC_* config.
    # e.g. {"chat": {"model": "mimo-v2.5"},
    #       "research": {"model": "qwen3.6-plus",
    #                    "base_url": "https://dashscope.aliyuncs.com/apps/anthropic",
    #                    "api_key": "sk-…"}}
    # NoDecode + the validator below parse the JSON ourselves so a malformed
    # value degrades to {} (MoA/routing off) instead of crashing Settings() —
    # this whole optional feature must never take down the agent.
    llm_roles: Annotated[dict, NoDecode] = {}
    # Mixture-of-Agents (JSON in LLM_MIXTURE). When >=2 `members` are given, the
    # listed `roles` (default pipeline/research/task/evolve) run MoA: every
    # member proposes an answer in parallel, then `aggregator` (default the
    # first member) synthesizes them into one. `layers` (default 1) adds refine
    # rounds. Each member/aggregator is {model, base_url?, api_key?}.
    # e.g. {"members": [{"model": "mimo-v2.5"}, {"model": "mimo-v2.5-pro"}],
    #       "aggregator": {"model": "mimo-v2.5-pro"}, "roles": ["pipeline"]}
    llm_mixture: Annotated[dict, NoDecode] = {}

    @field_validator("llm_roles", "llm_mixture", mode="before")
    @classmethod
    def _parse_json_dict(cls, value):
        """Tolerantly parse LLM_ROLES / LLM_MIXTURE from env JSON.

        A dict (kwargs / already parsed) passes through. A JSON string is
        parsed; anything malformed — the classic being a multi-line value in
        `.env` that dotenv truncates to its first physical line — degrades to
        `{}` with a warning rather than raising, so a broken optional routing
        config can never crash the agent. Note: multi-line JSON in `.env` must
        be wrapped in single quotes or it reaches us as just its first line."""
        if isinstance(value, dict):
            return value
        if value in (None, ""):
            return {}
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except (ValueError, TypeError):
                logging.getLogger("assistant").warning(
                    "ignoring malformed LLM_ROLES/LLM_MIXTURE JSON (%.60s…) — "
                    "multi-line values in .env must be wrapped in single quotes",
                    value.replace("\n", " "))
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    # GitHub
    github_token: str = ""
    github_user: str = ""

    # Chrome history collector (skipped gracefully when the file doesn't exist)
    chrome_history_path: Path = Path.home() / ".config/google-chrome/Default/History"
    # full titles/URLs enter prompts only for these domains …
    chrome_allowlist: list[str] = [
        "arxiv.org", "github.com", "huggingface.co", "openreview.net",
        "scholar.google.com", "docs.pytorch.org", "pytorch.org", "docs.vllm.ai",
        "developer.nvidia.com", "stackoverflow.com", "paperswithcode.com",
        "jiqizhixin.com", "qbitai.com", "buildkite.com", "overleaf.com",
    ]
    # … these are dropped at read time and never stored anywhere
    chrome_denylist: list[str] = [
        "bank", "alipay", "wealth", "insurance", "health", "hospital",
        "mail.google.com", "accounts.google.com",
    ]

    # Gmail collector — IMAP with the same app password as SMTP (headers only)
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    gmail_enabled: bool = True

    # Email delivery: Resend HTTP API first, SMTP fallback
    resend_api_key: str = ""
    resend_from: str = "onboarding@resend.dev"
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    digest_to: str = ""

    # Resume sync (M4) — Overleaf git-bridge URL (or any git remote); empty = disabled
    resume_remote_url: str = ""

    # Chat listener (`assistant chat-listen`) — the owner messages the agent and
    # gets a reply on the same channel. Email works out of the box (same IMAP/SMTP
    # creds); WeCom (企业微信 → WeChat plugin) needs an app + public callback URL.
    chat_poll_seconds: int = 60
    chat_subject_prefix: str = "agent"  # email subject must start with this
    wecom_corp_id: str = ""
    wecom_secret: str = ""
    wecom_agent_id: int = 0
    wecom_owner_userid: str = ""   # only this WeCom member may command the agent
    wecom_token: str = ""          # callback (receive) settings from the app's API page
    wecom_aes_key: str = ""        # 43-char EncodingAESKey
    wecom_callback_port: int = 8329

    # Finance ledger (finance.yaml in the profile repo) — chat-logged
    # income/expense records; summaries are computed in code
    finance_currency: str = "CNY"   # default currency for logged amounts

    # Vision (image understanding in chat). With a natively multimodal main
    # LLM (e.g. qwen3.6-plus), set llm_supports_images and chat attaches
    # images directly to the model call — no separate vision backend runs.
    # Otherwise images are described first via a remote multimodal API
    # (vision.py). Models never run locally (owner decision 2026-07-12).
    llm_supports_images: bool = False
    vision_api_key: str = ""
    vision_base_url: str = ""
    vision_model: str = ""
    vision_provider: str = "anthropic"  # anthropic | openai (OpenAI/Gemini/DashScope)
    vision_max_images: int = 3          # per message; extras are dropped

    # Local service daemon (`assistant serve`) — loopback-only HTTP consumed
    # by the OpenClaw bridge plugin and slash commands. The bearer token is
    # optional (the socket never leaves 127.0.0.1); set it to also keep other
    # local processes out.
    serve_port: int = 8377
    serve_token: str = ""
    serve_session_turns: int = 10   # exchanges of chat history kept per session
    chat_history_max_age_hours: int = 48  # context window: turns older than this
                                          # never enter a prompt (~2 days)
    chat_history_retention_days: int = 30  # disk retention: how long chat turns are
                                           # kept before the curate phase prunes them
                                           # (~1 month) — decoupled from the window above

    # Deliver-phase WeChat announce (best-effort, OFF by default — enable only
    # after removing --announce from the OpenClaw cron job, or 07:00 pings twice)
    wechat_announce: bool = False
    announce_channel: str = "openclaw-weixin"
    announce_account: str = ""     # gateway account id (…-im-bot)
    announce_to: str = ""          # owner's WeChat im id
    openclaw_bin: str = "/opt/node24/bin/openclaw"

    # Private website pages (todos/reading/routines) — when set, their content
    # is AES-GCM-encrypted at render time and unlocked in the browser with
    # this password (client-side crypto: the published HTML holds ciphertext)
    website_password: str = ""

    # Website marks sync: done/unrelated clicks queue in the browser and push
    # to this private repo; the agent pulls them each run. marks_push_token is
    # a fine-grained PAT scoped to ONLY this repo (Contents: RW) — it ships to
    # the browser inside the password-encrypted page payload, so scope it
    # minimally and never reuse GITHUB_TOKEN here.
    marks_repo: str = ""
    marks_push_token: str = ""

    # Personal website — "owner/name" GitHub Pages repo; the agent pushes the
    # rendered site directly to the default branch (owner's choice, 2026-07-02)
    website_repo: str = ""

    # Web search (chat `web_search` action + plan_task enrichment) — works
    # keyless via DuckDuckGo Lite. Preferred backends when keys are present:
    # Gemini grounding (one AI Studio key, search+answer in one call, free
    # 1500/day on 2.5-class) > Google CSE (key + cx; whole-web deprecated for
    # new engines since 2026-01) > Tavily > DDG.
    gemini_api_key: str = ""
    gemini_search_model: str = "gemini-2.5-flash"
    google_api_key: str = ""
    google_cse_id: str = ""   # Programmable Search Engine id ("cx")
    tavily_api_key: str = ""
    brave_api_key: str = ""

    # Research digest
    sources_file: Path = _REPO_ROOT / "config" / "sources.yaml"
    # window is wide because the seen-store dedupes across runs — a paper is
    # only ever surfaced the first day it appears
    arxiv_lookback_days: int = 7
    arxiv_max_per_query: int = 30
    research_top_papers: int = 10
    research_top_feed_items: int = 8

    # Data & run behavior
    data_dir: Path = Path.home() / ".personal-agent"
    lookback_hours: int = 26  # daily run with slack so nothing falls in a gap

    @property
    def cheap_model(self) -> str:
        """Model id for cheap/bulk calls: the haiku model if configured, else
        the main model."""
        return self.anthropic_default_haiku_model or self.anthropic_model

    @property
    def recipient(self) -> str:
        """Digest email recipient: explicit `digest_to`, falling back to the
        SMTP user (self-send)."""
        return self.digest_to or self.smtp_user

    @property
    def runs_dir(self) -> Path:
        """Directory holding per-run artifact/trace subdirectories."""
        return self.data_dir / "runs"

    @property
    def profile_dir(self) -> Path:
        """Directory of the git-versioned profile store (profile, todos, reading)."""
        return self.data_dir / "profile"

    @property
    def events_db(self) -> Path:
        """Path to the SQLite events/metrics database."""
        return self.data_dir / "events.db"

    @property
    def state_file(self) -> Path:
        """Path to the resume checkpoint written by `persist_state`."""
        return self.data_dir / "state.json"

    @property
    def resume_dir(self) -> Path:
        """Working directory for the resume/CV git clone."""
        return self.data_dir / "resume"
