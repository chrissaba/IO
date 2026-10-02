"""Event triggers that queue tasks: new files in a folder, and named webhooks.

Each trigger has a task template; placeholders are filled from the event:
    folder:  {file} {name}
    webhook: {payload} plus any top-level JSON field, e.g. {customer}
"""
import re
from pathlib import Path


def fill(template: str, values: dict) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: str(values.get(m.group(1), m.group(0))), template)


class FolderWatch:
    """Reports files that appear in a folder after the watch started (polling, no extra deps)."""

    def __init__(self) -> None:
        self.seen: dict[str, set[str]] = {}

    def new_files(self, trigger: dict) -> list[Path]:
        folder = Path(trigger.get("folder", ""))
        if not folder.is_dir():
            return []
        pattern = trigger.get("pattern") or "*"
        now = {str(p) for p in folder.glob(pattern) if p.is_file()}
        known = self.seen.get(trigger["id"])
        self.seen[trigger["id"]] = now
        if known is None:  # first look: only files added from now on count
            return []
        return sorted(Path(p) for p in now - known)
