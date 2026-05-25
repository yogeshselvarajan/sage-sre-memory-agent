from __future__ import annotations

# sage/agent.py
#
# SAGEAgent wraps the Strands Agent with a CLI run loop, slash commands, session
# snapshot management, and the human-in-the-loop interrupt handler.

import datetime
import sys

try:
    from rich.console import Console as _RichConsole
    _RICH = True
except ImportError:
    _RICH = False

from strands import Agent, ModelRetryStrategy
from strands.agent.conversation_manager import SlidingWindowConversationManager

from sage.config import AgentConfig, USER_ID, VAULT_DIR
from sage.hooks import ObservabilityPlugin, SREApprovalHook
from sage.prompts import SYSTEM_PROMPT, VAULT_REPORT_PROMPT
from sage.session import (
    build_session_manager,
    load_snapshot,
    make_session_id,
    save_snapshot,
)
from sage.tools import ALL_TOOLS
from sage.ui import StreamHandler, Terminal


class SAGEAgent:
    def __init__(
        self,
        config: AgentConfig | None = None,
        session_id: str | None = None,
        resume: bool = False,
    ) -> None:
        config = config or AgentConfig()
        self._config = config
        self._obs = ObservabilityPlugin()
        self._stream = StreamHandler()
        self._terminal = Terminal()
        self._turns = 0
        self._session_id = session_id or make_session_id()
        self._pre_exec_snapshot_saved = False

        # agent_ref is a 1-element list filled after Agent() returns.
        # SREApprovalHook needs the Agent for session snapshots, but Agent requires
        # the hook at construction time - this holder breaks the circular dependency.
        self._agent_ref: list = []

        approval_hook = SREApprovalHook(self._agent_ref)
        session_mgr = build_session_manager(self._session_id, config.agent_id)

        self._agent = Agent(
            model=config.build_model(),
            tools=ALL_TOOLS,
            system_prompt=SYSTEM_PROMPT,
            callback_handler=self._stream,
            conversation_manager=SlidingWindowConversationManager(
                window_size=config.window_size,
                should_truncate_results=True,
            ),
            hooks=[self._obs, approval_hook],
            agent_id=config.agent_id,
            name=config.agent_name,
            description=config.agent_description,
            retry_strategy=ModelRetryStrategy(max_attempts=5, initial_delay=2.0, max_delay=60.0),
            session_manager=session_mgr,
            state={
                "session_id": self._session_id,
                "environment": config.environment,
                "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            },
        )

        self._agent_ref.append(self._agent)

        if resume:
            loaded = load_snapshot(self._agent, self._session_id, "pre-execution")
            if not loaded:
                load_snapshot(self._agent, self._session_id, "start")

    def _vault_count(self) -> int:
        try:
            from mem0 import Memory
            from sage.tools.memory import _build_mem_config

            mem_config = _build_mem_config()
            m = Memory.from_config(config_dict=mem_config)
            raw = m.get_all(user_id=USER_ID)
            entries = raw.get("results", []) if isinstance(raw, dict) else (raw or [])
            return len(entries)
        except BaseException:
            return 0

    def run(self, resume: bool = False) -> None:
        vault_count = self._vault_count()
        self._terminal.welcome_rich(vault_count, self._session_id)

        if not resume:
            save_snapshot(self._agent, self._session_id, "start")

        while True:
            try:
                user_input = input("\nYou: ").strip()
                if not user_input:
                    continue

                if user_input.lower() in ("/quit", "/exit", "quit", "exit"):
                    self._quit()
                    break

                if user_input.lower() == "/rollback":
                    self._rollback()
                    continue

                if user_input.lower() == "/vault":
                    self._show_vault()
                    continue

                if user_input.lower().startswith("/recall "):
                    query = user_input[8:].strip()
                    self._explicit_recall(query)
                    continue

                if user_input.lower() == "/history":
                    self._show_history()
                    continue

                if user_input.lower() == "/reset":
                    try:
                        self._agent.conversation_manager.clear_history(self._agent.messages)
                    except (AttributeError, TypeError):
                        self._agent.messages.clear()
                    self._terminal.info("Conversation cleared. Vault knowledge persists.")
                    continue

                if user_input.lower().startswith("/export"):
                    parts = user_input.split(None, 1)
                    export_path = parts[1].strip() if len(parts) > 1 else "sage-vault-export.json"
                    self._export_vault(export_path)
                    continue

                if user_input.lower() == "/help":
                    self._terminal.welcome_rich(self._vault_count(), self._session_id)
                    continue

                self._turns += 1
                self._obs.reset_turn()
                print("\nSAGE: ", end="", flush=True)

                # invocation_state passes per-call metadata into the agent context.
                # Strands deprecated passing arbitrary **kwargs directly; this is the
                # correct way to supply runtime state to tools and hooks.
                result = self._agent(
                    user_input,
                    invocation_state={
                        "user_id": USER_ID,
                        "environment": self._config.environment,
                        "session_id": self._session_id,
                    },
                )
                print()

                result = self._handle_interrupts(result)

                if self._obs.last_turn_vault_miss:
                    self._maybe_suggest_store(user_input)

            except (KeyboardInterrupt, EOFError):
                print()
                self._quit()
                break
            except Exception as e:
                self._terminal.error(f"Error: {e}")

    def _handle_interrupts(self, result: object) -> object:
        while getattr(result, "stop_reason", None) == "interrupt":
            interrupts = getattr(result, "interrupts", []) or []
            responses: list[dict] = []

            print()
            for interrupt in interrupts:
                reason = getattr(interrupt, "reason", {}) or {}
                self._terminal.show_interrupt(reason)

                save_snapshot(
                    self._agent,
                    self._session_id,
                    "pre-execution",
                    extra={"interrupt_reason": reason},
                )
                self._pre_exec_snapshot_saved = True

                try:
                    if sys.stdin.isatty():
                        answer = input("\n  Approve execution? [y/N]: ").strip()
                    else:
                        answer = "n"
                except (EOFError, KeyboardInterrupt):
                    answer = "n"
                    print()

                responses.append({
                    "interruptResponse": {
                        "interruptId": interrupt.id,
                        "response": answer,
                    }
                })

            print("\nSAGE: ", end="", flush=True)
            result = self._agent(
                responses,
                invocation_state={
                    "user_id": USER_ID,
                    "environment": self._config.environment,
                    "session_id": self._session_id,
                },
            )
            print()

        return result

    def _rollback(self) -> None:
        if not self._pre_exec_snapshot_saved:
            self._terminal.info("No pre-execution snapshot available to roll back to.")
            return
        restored = load_snapshot(self._agent, self._session_id, "pre-execution")
        if restored:
            self._terminal.info("Rolled back to state before last approved runbook step.")
        else:
            self._terminal.info("Rollback snapshot not found.")

    def _show_vault(self) -> None:
        print("\nSAGE: ", end="", flush=True)
        self._agent(VAULT_REPORT_PROMPT)
        print()

    def _explicit_recall(self, query: str) -> None:
        # Bypass the agent entirely - query FAISS directly.
        # m.search() uses Nova Lite to LLM-filter vector results. If Nova Lite
        # is unavailable that filtering step silently returns empty. We therefore
        # fall back to get_all() + keyword matching so /recall always works.
        try:
            from mem0 import Memory
            from sage.tools.memory import _build_mem_config, _normalize_memories

            m = Memory.from_config(config_dict=_build_mem_config())

            # 1 - semantic vector search (works when Nova Lite is available)
            entries: list = []
            try:
                raw = m.search(query=query, user_id=USER_ID, limit=5)
                entries = raw.get("results", []) if isinstance(raw, dict) else (raw or [])
            except Exception:
                entries = []

            # 2 - keyword fallback: get_all + any query word in memory text
            if not entries:
                all_raw = m.get_all(user_id=USER_ID)
                all_entries = _normalize_memories(all_raw)
                query_words = query.lower().split()
                entries = [
                    e for e in all_entries
                    if any(w in (e.get("memory", "") or "").lower() for w in query_words)
                ]

            if not entries:
                print(f"\nSAGE: [VAULT EMPTY] No memories found for: {query}")
                return

            print(f"\nSAGE: [FROM VAULT] Found {len(entries)} match(es) for '{query}':\n")
            for i, e in enumerate(entries, 1):
                score = e.get("score", e.get("similarity", ""))
                score_str = f" (similarity: {score:.3f})" if isinstance(score, float) else ""
                meta = e.get("metadata") or {}
                cat = meta.get("category", "general")
                severity = meta.get("severity", "")
                severity_tag = f" [{severity}]" if severity else ""
                print(f"  {i}. [{cat.upper()}]{severity_tag}{score_str}")
                print(f"     {e.get('memory', str(e))}")
                print()
        except Exception as ex:
            print(f"\nSAGE: [ERROR] Recall failed: {ex}")

    def _show_history(self) -> None:
        messages = self._agent.messages[-6:] if self._agent.messages else []
        if not messages:
            print("No conversation history yet.")
            return
        for msg in messages:
            role = "You" if msg.get("role") == "user" else "SAGE"
            content = msg.get("content", "")
            if isinstance(content, list):
                text_parts = [b.get("text", "") for b in content if isinstance(b, dict) and "text" in b]
                content = " ".join(text_parts)
            if content:
                print(f"{role}: {str(content)[:200]}")

    def _maybe_suggest_store(self, original_question: str) -> None:
        # Guard: only prompt when running interactively. Piped input (tests, CI)
        # would otherwise hang on input() or consume the next command as the answer.
        if not sys.stdin.isatty():
            return
        try:
            if _RICH:
                from rich.console import Console
                Console().print(
                    "\n[dim]SAGE answered without vault support. Store this answer for next time? [y/N]:[/dim] ",
                    end="",
                )
            else:
                print("\n[Store this answer for next time? y/N]: ", end="", flush=True)
            choice = input().strip().lower()
            if choice == "y":
                print("\nSAGE: ", end="", flush=True)
                self._agent(
                    f"The user wants to store your answer to '{original_question}' in the vault. "
                    f"Summarize the key operational fact as a concise runbook entry and store it "
                    f"using mem0_memory with appropriate metadata (category, verified_date=today)."
                )
                print()
        except (EOFError, KeyboardInterrupt):
            pass

    def _export_vault(self, path: str) -> None:
        import json

        from sage.tools.memory import get_all_memories

        try:
            entries = get_all_memories()
            export = {
                "exported_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "agent": "SAGE - System Architecture and Guidance Engine",
                "user_id": USER_ID,
                "vault_path": str(VAULT_DIR),
                "entry_count": len(entries),
                "entries": [],
            }
            for e in entries:
                meta = e.get("metadata") or {}
                export["entries"].append({
                    "id": e.get("id"),
                    "memory": e.get("memory", ""),
                    "category": meta.get("category", "general"),
                    "verified_date": meta.get("verified_date"),
                    "severity": meta.get("severity"),
                    "created_at": e.get("created_at"),
                })
            export["entries"].sort(key=lambda x: (x.get("category") or "z", x.get("verified_date") or ""))

            with open(path, "w", encoding="utf-8") as f:
                json.dump(export, f, indent=2, ensure_ascii=False)

            self._terminal.info(f"Vault exported: {len(entries)} entries -> {path}")
        except Exception as e:
            self._terminal.error(f"Export failed: {e}")

    def _quit(self) -> None:
        usage = None
        try:
            result = self._agent("Goodbye.")
            if hasattr(result, "metrics") and hasattr(result.metrics, "accumulated_usage"):
                usage = result.metrics.accumulated_usage
        except Exception:
            pass

        self._terminal.session_summary(
            turns=self._turns,
            stores=self._obs.memory_stores,
            retrieves=self._obs.memory_retrieves,
            tool_calls=self._obs.tool_calls,
            usage=usage,
        )
        print("\nVault persists at: .vault/  (data survives restarts)")
