from __future__ import annotations

import datetime
import os

from strands import tool
from strands_tools import mem0_memory  # noqa: F401 - re-exported in ALL_TOOLS

from sage.config import MEMORY_CATEGORIES, SRE_FACT_EXTRACTION_PROMPT, USER_ID, VAULT_DIR

# Import the SRE prompt from config (defined there so Patch 1 can inject it into
# ALL Memory instances, including those created by strands_tools.mem0_memory).
_SRE_FACT_EXTRACTION_PROMPT = SRE_FACT_EXTRACTION_PROMPT


def _build_mem_config() -> dict:
    return {
        "vector_store": {
            "provider": "faiss",
            "config": {
                "embedding_model_dims": 1024,
                "path": str(VAULT_DIR),
            },
        },
        "embedder": {
            "provider": "aws_bedrock",
            "config": {
                "model": "amazon.titan-embed-text-v2:0",
                "aws_region": os.environ.get("AWS_REGION", "us-east-1"),
            },
        },
        "llm": {
            "provider": "aws_bedrock",
            "config": {
                "model": "us.amazon.nova-lite-v1:0",
                "temperature": 0.1,
                "max_tokens": 2000,
            },
        },
        "custom_fact_extraction_prompt": _SRE_FACT_EXTRACTION_PROMPT,
    }


def _age_label(verified_date_str: str | None, created_at: str | None = None) -> str:
    # Use mem0's created_at (set at write time) as primary source - it is always
    # accurate. verified_date comes from the LLM which may guess the wrong year,
    # producing false STALE warnings immediately after storage.
    date_str = (created_at or "")[:10] if created_at else None
    if not date_str:
        date_str = (verified_date_str or "")[:10] or None
    if not date_str:
        return "age unknown"
    try:
        d = datetime.date.fromisoformat(date_str)
        days = (datetime.date.today() - d).days
        if days < 0:
            return "just stored"
        if days <= 7:
            return f"{days}d old"
        elif days <= 30:
            return f"{days}d old"
        else:
            return f"STALE ({days}d old - verify)"
    except ValueError:
        return "age unknown"


def _normalize_memories(raw: object) -> list[dict]:
    """Normalize mem0 get_all() return value - handles both list and dict shapes."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        return raw.get("results", [])
    return []


def get_all_memories() -> list[dict]:
    """Direct FAISS read - returns raw memory list. Used by agent for export."""
    try:
        from mem0 import Memory

        m = Memory.from_config(config_dict=_build_mem_config())
        result = m.get_all(user_id=USER_ID)
        return _normalize_memories(result)
    except Exception:
        return []


@tool
def vault_summary() -> str:
    """Returns a grouped inventory of all entries stored in the SAGE knowledge vault.
    Groups entries by category and flags stale entries older than 30 days.
    """
    try:
        from mem0 import Memory

        m = Memory.from_config(config_dict=_build_mem_config())
        all_memories = _normalize_memories(m.get_all(user_id=USER_ID))

        if not all_memories:
            return "Knowledge vault is empty. No operational knowledge stored yet."

        count = len(all_memories)
        by_category: dict[str, list] = {}
        for entry in all_memories:
            cat = entry.get("metadata", {}).get("category", "general") if entry.get("metadata") else "general"
            by_category.setdefault(cat, []).append(entry)

        lines = [f"SAGE Knowledge Vault - {count} entr{'y' if count == 1 else 'ies'}\n"]

        for cat in sorted(by_category.keys()):
            entries = by_category[cat]
            cat_label = MEMORY_CATEGORIES.get(cat, cat).upper()
            lines.append(f"[{cat.upper()}] {cat_label} ({len(entries)})")

            for entry in entries:
                text = entry.get("memory", str(entry))
                meta = entry.get("metadata") or {}
                verified = meta.get("verified_date")
                severity = meta.get("severity", "")
                age = _age_label(verified, entry.get("created_at"))
                severity_tag = f" [{severity}]" if severity else ""
                lines.append(f"  - {text[:120]}{severity_tag} | {age}")

            lines.append("")

        stale_count = sum(
            1 for e in all_memories
            if "STALE" in _age_label(
                (e.get("metadata") or {}).get("verified_date"),
                e.get("created_at"),
            )
        )
        if stale_count:
            lines.append(f"WARNING: {stale_count} entr{'y' if stale_count == 1 else 'ies'} may be stale - verify before use in production")

        return "\n".join(lines)
    except Exception as e:
        return f"Could not read vault: {e}"
