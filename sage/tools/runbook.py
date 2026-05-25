from __future__ import annotations

from strands import tool


@tool
def apply_runbook_step(
    runbook_id: str,
    step_number: int,
    step_description: str,
    severity: str = "P3",
    environment: str = "staging",
) -> str:
    """Simulate executing a specific step from a stored operational runbook.
    Use this when the engineer asks to apply, execute, or run a runbook step.

    Args:
        runbook_id: The runbook identifier from the vault (e.g. RB-2024-47).
        step_number: Which step to execute, 1-indexed.
        step_description: Full description of the step being executed.
        severity: Runbook severity level. P1 = critical production, P2 = high, P3 = normal.
        environment: Target environment. One of: production, staging, dev.
    """
    env_tag = f"[{environment.upper()}]" if environment == "production" else f"[{environment}]"
    sev_tag = f"[{severity}]" if severity in ("P1", "P2") else ""
    return (
        f"{env_tag}{sev_tag} Step {step_number} of {runbook_id} executed: {step_description}\n"
        f"Status: SIMULATED SUCCESS - verify in your actual environment before marking complete."
    )
