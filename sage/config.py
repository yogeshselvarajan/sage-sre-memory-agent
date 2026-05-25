from __future__ import annotations

# sage/config.py
#
# Central configuration and two surgical monkeypatches that make SAGE work with
# the installed versions of strands_tools and mem0.
#
# Patch 1 - FAISS path redirect:
#   strands_tools.mem0_memory defaults to writing its FAISS index at /tmp/mem0_384_faiss.
#   That path is ephemeral on Windows and in containers. We redirect it to .vault/ so the
#   index survives restarts and can live alongside the repo.
#
# Patch 2 - mem0 AWSBedrockLLM fix for Amazon Nova models:
#   mem0 v0.1.118 has two bugs when using Nova via the Bedrock Converse API:
#   (a) _format_messages_amazon sends message content as a plain string, but Converse
#       requires [{text: "..."}] content blocks.
#   (b) After calling client.converse(), the code calls _parse_response() which tries
#       response.get("body").read() - Converse responses have no "body" key (only
#       invoke_model does). This causes an AttributeError.
#   (c) Sending both temperature and topP together raises ValidationException on Nova
#       (mem0 Bug B / GitHub issue #3891).
#   The patch replaces _generate_standard entirely for nova models to use the correct
#   Converse API format and response extraction.

import os
from dataclasses import dataclass
from pathlib import Path

from botocore.config import Config
from strands.models import BedrockModel

VAULT_DIR = Path(__file__).parent.parent / ".vault"
VAULT_DIR.mkdir(exist_ok=True)

SESSIONS_DIR = Path(__file__).parent.parent / ".sage-sessions"
SESSIONS_DIR.mkdir(exist_ok=True)

USER_ID = "sage-ops-001"

MEMORY_CATEGORIES = {
    "runbook":    "Step-by-step operational procedure",
    "decision":   "Architectural or technology choice with rationale",
    "incident":   "Post-incident learning or root cause",
    "constraint": "Hard limit - cost cap, compliance rule, quota",
    "ownership":  "Service owner, on-call, or team mapping",
    "quirk":      "Non-obvious behavior, gotcha, or tribal knowledge",
}

# SRE-specific fact extraction prompt.
# Defined here (not in tools/memory.py) so Patch 1 can inject it into EVERY
# Memory instance in the process - including those created by strands_tools.mem0_memory
# which builds its own Memory from env vars and never calls _build_mem_config().
# mem0's default FACT_RETRIEVAL_PROMPT is designed for personal memory (food, hobbies,
# names) and correctly rejects SRE runbook steps as "not personal facts" -> {"facts": []}.
SRE_FACT_EXTRACTION_PROMPT = """You are an SRE Knowledge Extractor. Extract ALL operational and technical facts from the given conversation.

Extract these as individual facts:
- Shell commands with context (AWS CLI, kubectl, docker, bash, terraform, etc.)
- Incident details: severity, affected service, timestamp, root cause, resolution
- Configuration values: thresholds, limits, connection counts, timeouts, quotas
- Service and infrastructure: service names, cluster names, regions, endpoints
- Ownership and on-call: team names, Slack channels, escalation paths
- Architectural decisions and rationale
- Known issues, workarounds, and operational gotchas
- Any technical procedure, runbook step, or operational fact

Rules:
- Extract facts ONLY from user messages
- Each fact is a standalone self-contained string
- Preserve exact command syntax and configuration values
- Return ONLY valid JSON: {"facts": ["fact1", "fact2"]} or {"facts": []} if nothing operational found

Examples:

Input:
user: hello
{"facts": []}

Input:
user: ECS DRAINING fix: aws ecs update-service --cluster prod-cluster --service my-service --force-new-deployment
{"facts": ["ECS DRAINING fix: aws ecs update-service --cluster prod-cluster --service my-service --force-new-deployment"]}

Input:
user: SEV2 on payment-service at 14:32 UTC. Root cause: DB connection pool exhaustion. Fix: max_connections 100 -> 250 on prod-db-01.
{"facts": ["payment-service SEV2 at 14:32 UTC", "Root cause: DB connection pool exhaustion on prod-db-01", "Fix: max_connections 100->250 on prod-db-01"]}

Input:
user: api-gateway CPU alert fires at 85% for 5 minutes, pages #sre-oncall
{"facts": ["api-gateway CPU alert: 85% for 5 minutes", "api-gateway alert pages #sre-oncall"]}

Input:
user: Restart auth service: kubectl rollout restart deployment/auth-service -n production
{"facts": ["auth-service restart: kubectl rollout restart deployment/auth-service -n production"]}

Input:
user: prod-db-01 RDS is in us-east-1, owned by platform team, PagerDuty rotation: platform-oncall
{"facts": ["prod-db-01 region: us-east-1", "prod-db-01 owner: platform team", "prod-db-01 on-call: PagerDuty platform-oncall"]}"""


# --- Patch 1: Redirect FAISS index from /tmp to .vault/ AND inject SRE prompt ---
#
# Mem0ServiceClient._append_faiss_config is called every time a mem0 Memory object is
# created from config - both by strands_tools.mem0_memory (env-var config) and by our
# direct Memory.from_config() calls. Patching here at class level covers both paths.
#
# We also inject custom_fact_extraction_prompt here so the strands_tools mem0_memory
# tool uses the SRE prompt instead of the default consumer prompt. Without this,
# mem0 rejects all runbook steps as "not personal facts" and stores nothing.
try:
    from strands_tools.mem0_memory import Mem0ServiceClient as _Mem0Client

    _original_append_faiss = _Mem0Client._append_faiss_config

    def _patched_faiss(self: object, config: dict | None = None) -> dict:
        result = _original_append_faiss(self, config)
        result["vector_store"]["config"]["path"] = str(VAULT_DIR)
        # Inject SRE prompt if not already set - applies to ALL Memory instances
        if "custom_fact_extraction_prompt" not in result:
            result["custom_fact_extraction_prompt"] = SRE_FACT_EXTRACTION_PROMPT
        return result

    _Mem0Client._append_faiss_config = _patched_faiss  # type: ignore[method-assign]
except (ImportError, AttributeError):
    pass


# --- Patch 2: Fix AWSBedrockLLM._generate_standard for Amazon Nova models ---
#
# Factory function pattern is required here: a plain nested def would capture
# _original_generate_standard as a late-binding free variable, meaning it would
# resolve at call time (when it has already been replaced). The factory captures
# the original as an argument in its own scope, forming a proper closure.
def _make_nova_generate_standard_patch(original):  # type: ignore[return]
    def _patched(self: object, messages: list, stream: bool = False) -> str:
        provider = getattr(self, "provider", "")
        cfg = getattr(self, "config", None)
        model_id: str = cfg.model if cfg else ""
        model_config: dict = getattr(self, "model_config", {}) or {}

        if provider == "amazon" and "nova" in model_id.lower():
            # Separate system messages from user/assistant turns.
            # Converse API takes system as a top-level list, not as a role in messages[].
            system_blocks: list = []
            formatted: list = []
            for msg in messages:
                role = msg.get("role", "user")
                content = str(msg.get("content", ""))
                if role == "system":
                    system_blocks.append({"text": content})
                elif role in ("user", "assistant"):
                    # Converse API requires [{text: "..."}] content blocks, not a plain string.
                    formatted.append({"role": role, "content": [{"text": content}]})

            if not formatted:
                formatted = [{"role": "user", "content": [{"text": "respond"}]}]

            # Omit topP - sending temperature+topP together triggers ValidationException
            # on Nova (confirmed mem0 Bug B, GitHub issue #3891).
            converse_params: dict = {
                "modelId": model_id,
                "messages": formatted,
                "inferenceConfig": {
                    "maxTokens": int(model_config.get("max_tokens", 2000)),
                    "temperature": float(model_config.get("temperature", 0.1)),
                },
            }
            if system_blocks:
                converse_params["system"] = system_blocks

            client = getattr(self, "client", None)
            if client is None:
                return ""
            response = client.converse(**converse_params)
            try:
                text = response["output"]["message"]["content"][0]["text"]
                import re as _re, json as _json
                # mem0 uses two LLM calls per add():
                #   1. Fact extraction  -> expects {"facts": [...]}   (object)
                #   2. Update comparison -> expects [{...}, {...}]     (array)
                # The regex must match BOTH shapes. Matching only \{...\} causes
                # the update-comparison response to fall through as raw text,
                # json.loads() fails with "Expecting value", and the entry is
                # silently dropped from FAISS even though SAGE said [STORED IN VAULT].
                json_match = _re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)
                if json_match:
                    candidate = json_match.group(0)
                    try:
                        _json.loads(candidate)
                        return candidate
                    except ValueError:
                        pass
                # Fallback: strip markdown code fences and return whatever is left.
                text = _re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=_re.IGNORECASE)
                text = _re.sub(r"\s*```$", "", text.strip())
                return text
            except (KeyError, IndexError, TypeError):
                return str(response)

        return original(self, messages, stream)
    return _patched


try:
    from mem0.llms.aws_bedrock import AWSBedrockLLM as _AWSBedrockLLM
    _AWSBedrockLLM._generate_standard = _make_nova_generate_standard_patch(  # type: ignore[method-assign]
        _AWSBedrockLLM._generate_standard
    )
except (ImportError, AttributeError):
    pass


@dataclass
class AgentConfig:
    model_id: str = "us.amazon.nova-pro-v1:0"
    region_name: str = "us-east-1"
    temperature: float = 0.1
    max_tokens: int = 4096
    window_size: int = 30
    agent_name: str = "SAGE"
    agent_description: str = "Operational knowledge vault - stores and recalls runbooks, architectural decisions, and incident learnings"
    agent_id: str = "sage-runbook-v1"
    environment: str = "production"

    def build_model(self) -> BedrockModel:
        kwargs: dict = dict(
            model_id=self.model_id,
            region_name=self.region_name,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            boto_client_config=Config(
                retries={"max_attempts": 5, "mode": "adaptive"},
                connect_timeout=10,
                read_timeout=300,
            ),
        )
        # Guardrail is optional - loaded from .env.guardrail at startup and validated
        # against the live AWS account. Cleared if the guardrail ID is not found.
        guardrail_id = os.environ.get("SAGE_GUARDRAIL_ID")
        guardrail_version = os.environ.get("SAGE_GUARDRAIL_VERSION", "1")
        if guardrail_id:
            kwargs["guardrail_id"] = guardrail_id
            kwargs["guardrail_version"] = guardrail_version
            kwargs["guardrail_trace"] = "enabled"
            kwargs["guardrail_latest_message"] = True
        return BedrockModel(**kwargs)
