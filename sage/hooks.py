from __future__ import annotations

# sage/hooks.py
#
# Two Strands HookProviders:
#
# ObservabilityPlugin - passive listener that records timing and memory counters.
#   Detects "vault miss" turns (agent answered without reading from memory) so the
#   UI can prompt the engineer to store the answer for next time.
#
# SREApprovalHook - active gate that pauses execution of dangerous runbook steps.
#   Uses Strands Interrupts to halt the agent loop and wait for human approval.
#   The agent resumes with the engineer's y/n response injected back as a message.

import time

from strands.hooks import (
    AfterInvocationEvent,
    AfterModelCallEvent,
    AfterToolCallEvent,
    BeforeInvocationEvent,
    BeforeModelCallEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)


class ObservabilityPlugin(HookProvider):
    def __init__(self) -> None:
        self._timers: dict[str, float] = {}
        self._turn_start: float = 0.0
        self.tool_calls: list[dict] = []
        self.memory_stores: int = 0
        self.memory_retrieves: int = 0
        self.memory_retrieve_hits: int = 0
        self.llm_calls: int = 0
        self.last_turn_vault_miss: bool = False
        self._current_turn_retrieved: bool = False

    def register_hooks(self, registry: HookRegistry, **kwargs: object) -> None:
        registry.add_callback(BeforeInvocationEvent, self._on_invocation_start)
        registry.add_callback(AfterInvocationEvent, self._on_invocation_end)
        registry.add_callback(BeforeModelCallEvent, self._before_llm)
        registry.add_callback(AfterModelCallEvent, self._after_llm)
        registry.add_callback(BeforeToolCallEvent, self._before_tool)
        registry.add_callback(AfterToolCallEvent, self._after_tool)

    def reset_turn(self) -> None:
        self._current_turn_retrieved = False
        self.last_turn_vault_miss = False

    def _on_invocation_start(self, event: BeforeInvocationEvent) -> None:
        self._turn_start = time.monotonic()

    def _on_invocation_end(self, event: AfterInvocationEvent) -> None:
        # A turn is a vault miss if no mem0_memory retrieve/list call was made.
        # This lets the UI offer to store the answer for future sessions.
        self.last_turn_vault_miss = not self._current_turn_retrieved

    def _before_llm(self, event: BeforeModelCallEvent) -> None:
        self._timers["__llm__"] = time.monotonic()
        self.llm_calls += 1

    def _after_llm(self, event: AfterModelCallEvent) -> None:
        self._timers.pop("__llm__", None)

    def _before_tool(self, event: BeforeToolCallEvent) -> None:
        name = event.tool_use.get("name", "unknown") if isinstance(event.tool_use, dict) else "unknown"
        self._timers[name] = time.monotonic()

    def _after_tool(self, event: AfterToolCallEvent) -> None:
        name = event.tool_use.get("name", "unknown") if isinstance(event.tool_use, dict) else "unknown"
        elapsed = time.monotonic() - self._timers.pop(name, time.monotonic())
        input_data = event.tool_use.get("input", {}) if isinstance(event.tool_use, dict) else {}
        action = input_data.get("action", "")

        entry = {"tool": name, "action": action, "elapsed_s": round(elapsed, 2)}
        self.tool_calls.append(entry)

        if name in ("mem0_memory", "vault_summary"):
            if action == "store":
                self.memory_stores += 1
            elif action in ("retrieve", "list"):
                self.memory_retrieves += 1
                self._current_turn_retrieved = True


# Tools that require human approval before execution.
INTERRUPT_TOOLS = {"apply_runbook_step"}

# Environments where every runbook step needs approval, regardless of severity.
INTERRUPT_ON_ENV = {"production"}


class SREApprovalHook(HookProvider):
    """Gates P1/P2 runbook steps in production with human-in-the-loop approval.

    Uses Strands Interrupts - pauses the agent loop until the engineer approves
    or rejects. The agent resumes with the engineer's response injected back.
    Session state is snapshotted before each interrupt so the run survives
    terminal crashes.
    """

    def __init__(self, agent_ref_holder: list) -> None:
        # agent_ref_holder is a 1-element list populated after Agent construction.
        # The Agent object doesn't exist when the hook is instantiated, so we use
        # this holder to break the circular dependency: Hook needs Agent for snapshots,
        # but Agent needs Hook at construction time.
        self._agent_ref = agent_ref_holder

    def register_hooks(self, registry: HookRegistry, **kwargs: object) -> None:
        registry.add_callback(BeforeToolCallEvent, self._gate_runbook_step)

    def _gate_runbook_step(self, event: BeforeToolCallEvent) -> None:
        if event.tool_use.get("name") not in INTERRUPT_TOOLS:
            return

        inputs = event.tool_use.get("input", {}) if isinstance(event.tool_use, dict) else {}
        severity = str(inputs.get("severity", "P3"))
        environment = str(inputs.get("environment", "staging"))

        requires_approval = severity in ("P1", "P2") or environment in INTERRUPT_ON_ENV

        if not requires_approval:
            return

        reason = {
            "tool": event.tool_use.get("name"),
            "runbook_id": inputs.get("runbook_id", "unknown"),
            "step_number": inputs.get("step_number", "?"),
            "step_description": inputs.get("step_description", ""),
            "severity": severity,
            "environment": environment,
        }

        # event.interrupt() pauses the Strands agent loop and surfaces an AgentResult
        # with stop_reason="interrupt". SAGEAgent._handle_interrupts() reads it, prompts
        # the engineer, and resumes the loop by passing the y/n response back to the agent.
        approval = event.interrupt("sre-runbook-approval", reason=reason)

        if str(approval).strip().lower() != "y":
            # Setting cancel_tool causes Strands to skip the tool call entirely
            # and return this string as the tool result to the model.
            event.cancel_tool = f"Runbook step rejected by engineer (severity={severity}, env={environment})"
