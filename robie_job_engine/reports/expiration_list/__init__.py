"""Weekly Expiration List report (EZLynx -> Google Sheet).

Server-side automation of the Claude-in-Chrome "Weekly Expiration List
Report (EZLynx -> Google Sheet)" procedure:

  Retention Center expiration list (<=30 days) -> per-account sidebar +
  PolicyCard + renewal-discussion reads -> latest staff note only
  (skip bots/automation) -> HTML-table layout into the "Expiration List"
  sheet tab (producer sections, yellow section rows, blue producer rows,
  green highlight for already-renewed).

Every behavior choice that needed an answer from the process owner
(Sandeep) is recorded in ASSUMPTIONS.md next to this package, mapped to
question numbers E1-E15 / X1-X6. Sandeep's answers are pending as of the
build date; nothing here assumes them.
"""

from .runner import main, run_report  # noqa: F401

__all__ = ["main", "run_report"]
