from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

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

    # Local service daemon (`assistant serve`) — loopback-only HTTP consumed
    # by the OpenClaw bridge plugin and slash commands. The bearer token is
    # optional (the socket never leaves 127.0.0.1); set it to also keep other
    # local processes out.
    serve_port: int = 8377
    serve_token: str = ""
    serve_session_turns: int = 10  # exchanges of chat history kept per session

    # Deliver-phase WeChat announce (best-effort, OFF by default — enable only
    # after removing --announce from the OpenClaw cron job, or 07:00 pings twice)
    wechat_announce: bool = False
    announce_channel: str = "openclaw-weixin"
    announce_account: str = ""     # gateway account id (…-im-bot)
    announce_to: str = ""          # owner's WeChat im id
    openclaw_bin: str = "/opt/node24/bin/openclaw"

    # Personal website — "owner/name" GitHub Pages repo; the agent pushes the
    # rendered site directly to the default branch (owner's choice, 2026-07-02)
    website_repo: str = ""

    # Web search (chat `web_search` action + plan_task enrichment) — works
    # keyless via DuckDuckGo Lite; set a Tavily key for a sturdier backend
    tavily_api_key: str = ""

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
        return self.anthropic_default_haiku_model or self.anthropic_model

    @property
    def recipient(self) -> str:
        return self.digest_to or self.smtp_user

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def profile_dir(self) -> Path:
        return self.data_dir / "profile"

    @property
    def events_db(self) -> Path:
        return self.data_dir / "events.db"

    @property
    def state_file(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def resume_dir(self) -> Path:
        return self.data_dir / "resume"
