"""The two v1 run surfaces (doc/BENCHMARKS.md §2.1/§2.6).

`role_probe` is the M layer: one prompt against whatever model an LLM_ROLES
role routes to — no agent code. `chat_turn` is the A layer: a full
`handle_turn` on the bench profile under the action sandbox (repair rounds
and retrieval-compose included by construction). There is deliberately no
phase surface: LangGraph nodes are nested closures that continue downstream
into publish/delivery."""

from dataclasses import dataclass, field

from assistant.bench.sandbox import SandboxRecorder, action_sandbox
from assistant.platform.config import Settings
from assistant.platform.llm import LLM


def role_probe(settings: Settings, role: str, prompt: str, llm: LLM | None = None,
               **kw) -> str:
    """M layer: raw completion on `role`'s configured route (mixture off —
    probes measure ONE model, not the MoA ensemble)."""
    llm = llm or LLM(settings)
    return llm.complete(prompt, role=role, mixture=False, **kw)


@dataclass
class TurnRecord:
    """Everything a scorer may inspect from one bench chat turn. `executed`
    entries are {"action", "outcome", "ok"} (ok=False when the outcome
    looks_failed); `faked` entries are {"action"}."""

    reply: str
    outcome: str
    executed: list[dict] = field(default_factory=list)
    faked: list[dict] = field(default_factory=list)

    def executed_types(self, ok_only: bool = False) -> list:
        return [e["action"].get("type") for e in self.executed
                if not ok_only or e.get("ok")]

    def faked_types(self) -> list:
        return [e["action"].get("type") for e in self.faked]

    def raw(self) -> dict:
        """The retained-for-rescoring payload (§2.7)."""
        return {"reply": self.reply, "executed": self.executed,
                "faked": self.faked}


def chat_turn(settings: Settings, llm, text: str,
              history: list[dict] | None = None,
              image_paths: list[str] | None = None) -> TurnRecord:
    """A layer: one owner message through the real `handle_turn`, actions
    confined to the sandbox, on the bench profile's scratch stores.
    `internal=False` deliberately — bench turns are owner-facing turns."""
    from assistant.agent.chat.agent import handle_turn

    recorder = SandboxRecorder()
    with action_sandbox(recorder):
        turn = handle_turn(text, settings, llm, history=history,
                           image_paths=image_paths)
    return TurnRecord(reply=turn.reply, outcome=turn.outcome,
                      executed=recorder.executed, faked=recorder.faked)
