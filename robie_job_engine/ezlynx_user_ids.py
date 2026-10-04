"""Confirmed numeric EZLynx user ids for the direct Task API.

Ported from streetsmart-phone-watchdog ``src/ezlynx/ezlynx_users.py``
(eca8cba, "Use confirmed EZLynx user ids on the direct task path").
Those ids came from EZLynx API task readbacks on 2026-10-03.

A login missing here has no confirmed id. The direct Task API refuses it
and the task goes through Zapier, which assigns by login. Never guess an
id: a wrong one assigns the task to someone else.
"""

from __future__ import annotations

# EZLynx login -> numeric EZLynx user id (``task.assignedUserId``).
EZLYNX_USER_IDS: dict[str, int] = {
    "a_illanes": 435794,
    "ahuntley": 407154,
    "Amber14": 436607,
    "AngieV": 386617,
    "Carlo1": 98248,
    "Daniela_Aguilar": 331403,
    "Diana12": 260229,
    "Erika11": 257762,
    "Gabrielac1": 253180,
    "Jackie_Arriola": 334413,
    "Jazmin11": 386733,
    "jferrara3": 99082,
    "Josecabrera": 307430,
    "KarlaSS": 356806,
    "Lperdomo1": 428230,
    "MariaB12": 307299,
    "MikeS1": 165362,
    "Ricardo2": 268013,
    "Sandy11": 311006,
    "SSRobie": 438318,
    "TCimei": 365744,
    "Zeus12": 307298,
}

# Logins on the agency roster with no confirmed id yet. Zapier only.
UNCONFIRMED_LOGINS: tuple[str, ...] = (
    "Alejandro11",
    "Amartinez21",
    "Eramos1",
    "Eunice",
    "Jimmy1",
    "Mancina1",
    "Markley1",
    "Mitch1",
    "Nmaldonado2",
    "SCanales",
    "SSNicole",
    "Sandeep11",
    "anaflores",
)

_BY_FOLDED = {login.casefold(): user_id for login, user_id in EZLYNX_USER_IDS.items()}


def ezlynx_user_id_for(login: str | None) -> int | None:
    """Numeric id for an EZLynx login, or None when it is not confirmed."""
    return _BY_FOLDED.get(str(login or "").strip().casefold())

