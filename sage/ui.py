from __future__ import annotations

import sys

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    _RICH = True
except ImportError:
    _RICH = False


class StreamHandler:
    def __call__(self, **kwargs: object) -> None:
        if "current_tool_use" in kwargs:
            ctu = kwargs["current_tool_use"]
            if isinstance(ctu, dict) and ctu.get("name") and not ctu.get("input"):
                tool_name = ctu["name"]
                if _RICH:
                    Console().print(f"\n  [dim cyan]calling {tool_name}...[/dim cyan]", end="")
                else:
                    print(f"\n  [{tool_name}]", end="", flush=True)
            return

        chunk = kwargs.get("data", "")
        if chunk:
            sys.stdout.write(str(chunk))
            sys.stdout.flush()


class Terminal:
    def __init__(self) -> None:
        self._console = Console() if _RICH else None

    def welcome(self, vault_count: int) -> None:
        if not _RICH or self._console is None:
            print("=" * 60)
            print("SAGE - System Architecture and Guidance Engine")
            print(f"Knowledge vault: {vault_count} entries loaded")
            print("=" * 60)
            return

        table = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 2))
        table.add_column("Command", style="cyan", width=18)
        table.add_column("Action")
        table.add_row("/vault", "List all stored knowledge entries")
        table.add_row("/recall <query>", "Explicit semantic search of vault")
        table.add_row("/history", "Show last 3 conversation turns")
        table.add_row("/reset", "Clear conversation history (vault persists)")
        table.add_row("/help", "Show this command list")
        table.add_row("/quit", "Exit with session summary")

        vault_status = (
            f"[bold green]{vault_count} entries[/bold green]"
            if vault_count > 0
            else "[dim]empty - start adding runbooks[/dim]"
        )

        self._console.print(
            Panel(
                f"[bold white]SAGE[/bold white] - System Architecture and Guidance Engine\n"
                f"[dim]Operational knowledge vault - powered by Bedrock Nova Pro + FAISS[/dim]\n\n"
                f"Knowledge vault: {vault_status}\n\n"
                + table.__str__(),
                title="[bold cyan]SAGE v1.0[/bold cyan]",
                border_style="cyan",
            )
        )

    def welcome_rich(self, vault_count: int, session_id: str = "") -> None:
        if not _RICH or self._console is None:
            self.welcome(vault_count)
            return

        table = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 2))
        table.add_column("Command", style="cyan", width=22)
        table.add_column("Action")
        table.add_row("/vault", "List all stored knowledge entries")
        table.add_row("/recall <query>", "Explicit semantic search of vault")
        table.add_row("/export [file]", "Export full vault to JSON (default: sage-vault-export.json)")
        table.add_row("/history", "Show last 3 conversation turns")
        table.add_row("/reset", "Clear conversation history (vault persists)")
        table.add_row("/help", "Show this command list")
        table.add_row("/quit", "Exit with session summary")
        table.add_row("/rollback", "Restore state to before last approved runbook step")
        table.add_row("--resume <id>", "Resume interrupted session: python main.py --resume <id>")

        vault_line = (
            f"[bold green]{vault_count} entries ready[/bold green]"
            if vault_count > 0
            else "[yellow]Empty vault - tell me your first runbook[/yellow]"
        )
        session_line = f"\n[dim]Session: {session_id}[/dim]" if session_id else ""

        panel_content = (
            "[bold white]SAGE[/bold white] - System Architecture and Guidance Engine\n"
            "[dim]Powered by Bedrock Nova Pro + FAISS persistent memory[/dim]\n\n"
            f"Knowledge vault: {vault_line}\n"
            f"{session_line}"
        )

        self._console.print(Panel(panel_content, title="[bold cyan]SAGE v2.0[/bold cyan]", border_style="cyan"))
        self._console.print(table)
        self._console.print()

    def session_summary(
        self,
        turns: int,
        stores: int,
        retrieves: int,
        tool_calls: list[dict],
        usage: dict | None,
    ) -> None:
        if not _RICH or self._console is None:
            print(f"\nSession: {turns} turns | {stores} stored | {retrieves} recalled")
            return

        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("Metric", style="cyan")
        table.add_column("Value")
        table.add_row("Conversation turns", str(turns))
        table.add_row("Memories stored", f"[green]{stores}[/green]")
        table.add_row("Vault retrievals", f"[blue]{retrieves}[/blue]")
        if usage:
            table.add_row("Input tokens", str(usage.get("inputTokens", "-")))
            table.add_row("Output tokens", str(usage.get("outputTokens", "-")))

        if tool_calls:
            self._console.print("\n[bold]Tool calls this session:[/bold]")
            for tc in tool_calls:
                self._console.print(f"  {tc['tool']} - {tc['elapsed_s']}s")

        self._console.print(Panel(table, title="[bold cyan]Session Summary[/bold cyan]", border_style="dim"))

    def show_interrupt(self, reason: dict) -> None:
        if _RICH and self._console:
            from rich.panel import Panel
            from rich.table import Table as RichTable

            t = RichTable(show_header=False, box=None, padding=(0, 2))
            t.add_column("Key", style="cyan")
            t.add_column("Value")
            t.add_row("Runbook", str(reason.get("runbook_id", "?")))
            t.add_row("Step", str(reason.get("step_number", "?")))
            t.add_row("Action", str(reason.get("step_description", ""))[:80])
            t.add_row("Severity", f"[red]{reason.get('severity', '?')}[/red]")
            t.add_row("Environment", f"[yellow]{reason.get('environment', '?')}[/yellow]")
            self._console.print()
            self._console.print(
                Panel(
                    t,
                    title="[bold red]EXECUTION PAUSED - Runbook Approval Required[/bold red]",
                    border_style="red",
                )
            )
        else:
            print(
                f"\n[APPROVAL REQUIRED] "
                f"Runbook={reason.get('runbook_id')} "
                f"Step={reason.get('step_number')} "
                f"Action={str(reason.get('step_description',''))[:60]} "
                f"Severity={reason.get('severity')} "
                f"Env={reason.get('environment')}"
            )

    def error(self, msg: str) -> None:
        if _RICH and self._console:
            self._console.print(f"[red]{msg}[/red]")
        else:
            print(f"[ERROR] {msg}")

    def info(self, msg: str) -> None:
        if _RICH and self._console:
            self._console.print(f"[dim]{msg}[/dim]")
        else:
            print(msg)
