from __future__ import annotations

# sage/session.py
#
# Session management: ID generation, FileSessionManager wiring, and agent snapshots.
#
# Snapshots use strands.types.Snapshot which is a @dataclass with to_dict()/from_dict()
# methods - NOT a Pydantic model. Using model_dump_json() or model_validate_json() will
# raise AttributeError. Always go through json.dumps(snap.to_dict()) and
# Snapshot.from_dict(json.loads(...)).
#
# Two snapshot phases are used:
#   "start"         - saved once at the beginning of each session (clean baseline)
#   "pre-execution" - saved just before the engineer approves a runbook step
#                     (allows /rollback to undo the approved action)

import datetime
import json
from pathlib import Path

from strands.session.file_session_manager import FileSessionManager
from strands.types import Snapshot

SESSIONS_DIR = Path(__file__).parent.parent / ".sage-sessions"
SESSIONS_DIR.mkdir(exist_ok=True)


def make_session_id() -> str:
    return f"sage-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"


def build_session_manager(session_id: str, agent_id: str) -> FileSessionManager:
    return FileSessionManager(
        session_id=session_id,
        storage_dir=str(SESSIONS_DIR),
    )


def save_snapshot(agent: object, session_id: str, phase: str, extra: dict | None = None) -> None:
    try:
        snap = agent.take_snapshot(  # type: ignore[attr-defined]
            preset="session",
            app_data={"phase": phase, "session_id": session_id, **(extra or {})},
        )
        path = SESSIONS_DIR / f"{session_id}-{phase}.json"
        path.write_text(json.dumps(snap.to_dict()), encoding="utf-8")
    except Exception:
        pass


def load_snapshot(agent: object, session_id: str, phase: str) -> bool:
    path = SESSIONS_DIR / f"{session_id}-{phase}.json"
    if not path.exists():
        return False
    try:
        snap = Snapshot.from_dict(json.loads(path.read_text(encoding="utf-8")))
        agent.load_snapshot(snap)  # type: ignore[attr-defined]
        return True
    except Exception:
        return False


def list_sessions() -> list[str]:
    try:
        # Extract base session IDs from "sage-YYYYMMDD-HHMMSS-start.json" filenames.
        names = {p.stem.rsplit("-", 1)[0] for p in SESSIONS_DIR.glob("sage-*-start.json")}
        return sorted(names, reverse=True)[:10]
    except Exception:
        return []
