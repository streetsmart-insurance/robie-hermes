"""One voice for Playground Chat and email.

The persona and the spoken lines live in ``playground_persona.md``.
Reply code loads them from that file so the two channels cannot drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

_PERSONA_PATH = Path(__file__).with_name("playground_persona.md")
_KEY = "["


@dataclass(frozen=True)
class Persona:
    prompt: str
    lines: dict[str, str]


def persona_path() -> Path:
    return _PERSONA_PATH


def load_persona(path: Path | None = None) -> Persona:
    raw = (path or _PERSONA_PATH).read_text(encoding="utf-8")
    prompt_lines: list[str] = []
    lines: dict[str, str] = {}
    current: str | None = None
    bucket: list[str] = []
    for row in raw.splitlines():
        if row.startswith(_KEY) and row.endswith("]") and len(row) > 2:
            if current is None:
                prompt_lines = bucket
            else:
                lines[current] = "\n".join(bucket).strip()
            current = row[1:-1].strip()
            bucket = []
            continue
        bucket.append(row)
    if current is None:
        prompt_lines = bucket
    else:
        lines[current] = "\n".join(bucket).strip()
    prompt = "\n".join(prompt_lines).strip()
    if not prompt:
        raise RuntimeError("Playground persona prompt is empty")
    return Persona(prompt=prompt, lines=lines)


def persona_text() -> str:
    return load_persona().prompt


def line(name: str) -> str:
    persona = load_persona()
    try:
        return persona.lines[name]
    except KeyError as exc:
        raise KeyError(f"Playground persona is missing [{name}]") from exc


def render_turn_prompt(
    *,
    channel: str,
    requested_by: str,
    text: str,
    memories: list,
) -> str:
    """The prompt for this turn. Deterministic code still decides the action."""
    parts = [
        persona_text(),
        "",
        f"Channel: {channel}",
        f"From: {requested_by or 'someone on the team'}",
        "",
        "Relevant memory:",
    ]
    if not memories:
        parts.append("- none")
    else:
        for item in memories:
            parts.append(
                "- ({scope} {kind}) {body} | team: {team} | client: {client} | applicant: {applicant} | outcome: {outcome} | job: {job}".format(
                    scope=item.scope,
                    kind=item.kind,
                    body=item.body,
                    team=getattr(item, "team", "") or "",
                    client=item.client_name or "",
                    applicant=getattr(item, "applicant_id", "") or "",
                    outcome=item.outcome or "",
                    job=item.job_id or "",
                )
            )
    parts.extend(
        [
            "",
            "Request:",
            str(text or "").strip(),
            "",
            "If the client, the field, or the value is unclear, ask one question. Do not guess. "
            "Memory does not override a block or the write allowlist.",
        ]
    )
    return "\n".join(parts)
