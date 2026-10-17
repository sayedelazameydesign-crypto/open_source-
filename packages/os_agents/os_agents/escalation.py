"""Escalation policy — when a task must *not* stay with the sub-agent.

The deferral note in ADR-0020 said auto-escalation needs a *measured*
difficulty signal before it earns its place. This module is that signal, and it
is deliberately assembled from instruments the platform already owns:

* :meth:`~os_core.router.ModelRouter.profile` — the same offline profiler that
  routes model tiers decides whether a task is planner-shaped (its
  ``_PLANNING_SIGNALS`` already read English *and* Arabic);
* a capability-fit regex — a read-only sub-agent asked to *create, fix, run*
  is being sent to the wrong level regardless of how smart it is;
* a token ceiling — a delegation unit that will not fit a small model's context
  is a routing error the orchestrator should hear about, not a context blow-up
  the sub-agent should discover;
* the run's own outcome — step/time bounds and resource refusals mean the
  sub-agent's attempts are spent; retrying it is the definition of insanity.

Three triggers, one shape: :class:`EscalationDecision` says *escalate or not*,
*which signal fired*, and *what the caller should do instead*. Rule-based and
deterministic — no extra model call is spent deciding whether a model call is
worth making (a learned difficulty scorer is explicitly deferred in ADR-0021).
"""

from __future__ import annotations

import re

from os_core.router import ModelRouter, RouterConfig, TaskProfile
from pydantic import BaseModel, Field

from os_agents.validation import CorrectionResult

#: Default ceiling for one delegation unit. A small local model (8B-class,
#: 8k context) must hold the task, the tool observations *and* the report;
#: 4096 tokens leaves room for all three without colliding with the router's
#: own 6000-token planner threshold — the two rules answer different questions.
DEFAULT_MAX_TASK_TOKENS = 4096

#: Mutation/execution verbs — work a read-only allow-list cannot do. Narrow on
#: purpose (same discipline as Guardrails' patterns): a verb plus its object,
#: English and Arabic, shadda and shadda-less spellings both. Bare ``run`` — not
#: ``running``/``runbook``, which ``\b`` already excludes — because "run the
#: tests" is execution just as much as "run the deployment".
_MANDATE_SIGNALS = re.compile(
    r"\b(write|create|edit|modify|update|fix|patch|delete|remove|deploy|commit|push|"
    r"merge|install|refactor|implement|execute)\b"
    # ``run`` needs an article: this platform's own nouns ("run outcome ledger",
    # "run store", "/runs") are read-only vocabulary, and a bare \brun\b would
    # flag every task that mentions them. "run the tests" still escalates; an
    # article-less execution ask fails the allow-list and escalates *after*.
    r"|\b((?:run|runs)\s+(?:the|a|an|these|those|our))\b"
    r"|(إنشاء|انشئ|أنشئ|تعديل|عدّل|عدل|تحديث|اكتب|احذف|أصلح|اصلاح|"
    r"نفذ|نفّذ|شغّل|شغل|انشر|ادمج|ثبّت|حرِّر|حرر)",
    re.IGNORECASE,
)

#: Which sub-agent outcome maps to which escalation signal.
_FAILURE_SIGNALS: dict[str, str] = {
    "step bound": "bounds",
    "time bound": "bounds",
    "resource shortage": "resource",
}


class EscalationDecision(BaseModel):
    """One routing verdict — evidence first, action spelled out."""

    escalate: bool
    signal: str = ""  # planning | mandate | task-size | bounds | resource | correction-loop
    reason: str = ""
    #: What the *main* agent should do now — a decision without an action is
    #: just a complaint.
    instruction: str = ""

    def render(self) -> str:
        return f"[{self.signal}] {self.reason}" if self.escalate else ""


class EscalationPolicy(BaseModel):
    """Rule set deciding pre-delegation fit and post-run escalation.

    Every switch is a knob because operators tune differently: a deployment
    that only wants exploration delegation turns ``planning_escalates`` on and
    ``mandate_escalates`` on (both defaults) and never writes a custom policy;
    a test or an operator who knows better passes ``force=True`` at the call
    site for a single exception.
    """

    planning_escalates: bool = True
    mandate_escalates: bool = True
    escalate_on_failure: bool = True
    max_task_tokens: int = Field(default=DEFAULT_MAX_TASK_TOKENS, ge=1)

    # -- pre-delegation --------------------------------------------------

    def precheck(self, task: str, *, router: ModelRouter | None = None) -> EscalationDecision:
        """Decide *before* inference whether this task belongs to the sub-agent.

        ``router`` is optional so a hub under test (or a caller that already
        holds a router) reuses its config; the default throwaway router keeps
        this function stateless and offline.
        """
        if not task.strip():
            return EscalationDecision(escalate=False)

        profile: TaskProfile = (router or ModelRouter(config=RouterConfig())).profile(task)

        if self.planning_escalates and profile.is_planning:
            return EscalationDecision(
                escalate=True,
                signal="planning",
                reason=(
                    "task is planner-shaped (planning signal or long context) — "
                    "planning stays with the orchestrator"
                ),
                instruction=(
                    "Handle this task in the main loop yourself; do not delegate it. "
                    "If you still need exploration fragments, split the task into "
                    "concrete read-only lookups first."
                ),
            )

        if self.mandate_escalates:
            match = _MANDATE_SIGNALS.search(task)
            if match:
                verb = next(g for g in match.groups() if g)
                return EscalationDecision(
                    escalate=True,
                    signal="mandate",
                    reason=(
                        f"task asks for {verb!r} — mutation/execution exceeds the "
                        "sub-agent's read-only allow-list"
                    ),
                    instruction=(
                        "Handle this task in the main loop yourself; a read-only "
                        "sub-agent cannot perform it."
                    ),
                )

        if profile.prompt_tokens > self.max_task_tokens:
            return EscalationDecision(
                escalate=True,
                signal="task-size",
                reason=(
                    f"task is {profile.prompt_tokens} tokens, above the delegation "
                    f"ceiling of {self.max_task_tokens}"
                ),
                instruction=(
                    "Handle this task in the main loop, or split it into smaller "
                    "read-only lookups before delegating."
                ),
            )

        return EscalationDecision(escalate=False)

    # -- post-run ---------------------------------------------------------

    def postcheck(
        self,
        *,
        ok: bool,
        stopped_reason: str = "",
        error: str = "",
    ) -> EscalationDecision:
        """Decide *after* a run whether its outcome requires the main loop.

        Primitive arguments on purpose: this policy must not import the
        sub-agent layer (nor be importable in a cycle by it) — the same
        discipline that keeps the platform's other cross-layer hooks duck-typed.
        """
        if ok or not self.escalate_on_failure:
            return EscalationDecision(escalate=False)
        signal = _FAILURE_SIGNALS.get(stopped_reason, "bounds")
        return EscalationDecision(
            escalate=True,
            signal=signal,
            reason=error or stopped_reason or "sub-agent did not complete the task",
            instruction=(
                "Handle this task in the main loop yourself — the sub-agent's "
                "attempts are spent (do not re-delegate the same task unchanged)."
            ),
        )

    # -- composition with the self-correction loop -------------------------

    @classmethod
    def from_correction(cls, result: CorrectionResult) -> EscalationDecision:
        """Map an exhausted :class:`~os_agents.validation.SelfCorrectionLoop`
        onto an escalation: repeated corrections *are* attempt exhaustion, and
        the next rung up the ladder is the main agent with a bigger model.
        """
        if result.accepted:
            return EscalationDecision(escalate=False)
        return EscalationDecision(
            escalate=True,
            signal="correction-loop",
            reason=(
                f"self-correction exhausted after {result.attempts} attempt(s) "
                "without an accepted payload"
            ),
            instruction=(
                "Escalate to the main agent with this feedback:\n" + result.report.feedback()
            ),
        )


__all__ = [
    "DEFAULT_MAX_TASK_TOKENS",
    "EscalationDecision",
    "EscalationPolicy",
]
