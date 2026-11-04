"""
Prompts live in files, not in code: prompts/<name>/<version>.md.

That gets you three things for free. Prompt changes show up as diffs in code
review. Every call can log exactly which version produced its output. And the
eval harness can run two versions side by side without touching the code.
"""

from dataclasses import dataclass
from functools import cache

from helpdesk.config import PROMPTS_DIR


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    text: str

    @property
    def id(self) -> str:
        return f"{self.name}/{self.version}"


@cache
def load_prompt(name: str, version: str) -> Prompt:
    path = PROMPTS_DIR / name / f"{version}.md"
    if not path.exists():
        available = sorted(p.stem for p in (PROMPTS_DIR / name).glob("*.md"))
        raise FileNotFoundError(f"No prompt {name}/{version}. Available versions: {available}")
    return Prompt(name=name, version=version, text=path.read_text(encoding="utf-8").strip())
