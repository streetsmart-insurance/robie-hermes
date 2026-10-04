"""Live destination ports for ascend_delivery_state.ReliableDelivery.

Each port answers the three questions the delivery state machine asks:

- ``find_for_event(event)``: what already exists for this source key, and
  which components are *authoritatively* absent (a complete read of every
  place this port writes found no marker for them).
- ``send_component(event, key, component)``: write one component, return
  ``{"ids": {component: id}}`` or ``{"ids": {}}`` when not confirmed.
- ``readback(ids)``: re-read each id and report its source key and binding,
  which ReliableDelivery compares against the event before anything counts.

Every write carries a source marker ``[ascend:<key>:<component>]`` so a
later run can find it without a local receipt.

EZLynx: notes and tasks go only to the applicant's ``Tasks by Robie``
discussion, through the direct Task API login and gates (allowlist +
driver lease, no phone numbers). That is the only place this port writes,
so reading every ``Tasks by Robie`` discussion is a complete search.

QBO: commission deposits with the mapped bank account, income account,
payee, amount, and currency. Writes are refused against the Production
company unless ``ROBIE_ASCEND_QBO_PRODUCTION_WRITES=1`` (never set by this
change); Test uses the QBO sandbox company.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from typing import Any, Optional
from urllib import parse, request

from . import ezlynx_task_api as tapi

MARKER_RE = re.compile(r"\[ascend:([^\]\s:]+(?::[^\]\s:]+)*):(note_id|task_id|deposit_id)\]")
QBO_PRODUCTION_WRITES_ENV = "ROBIE_ASCEND_QBO_PRODUCTION_WRITES"
QBO_PAGE = 500


def marker(key: str, component: str) -> str:
    return f"[ascend:{key}:{component}]"


def parse_marker(text: Any) -> tuple[str, str]:
    """(source key, component) from the last marker in ``text``, or ("", "")."""
    found = MARKER_RE.findall(str(text or ""))
    return found[-1] if found else ("", "")


# ---------------------------------------------------------------------------
# EZLynx
# ---------------------------------------------------------------------------


class EZLynxAscendDestination:
    """Notes and tasks on the applicant's ``Tasks by Robie`` discussion."""

    def __init__(self, *, urlopen: Any = None, accessor: Any = None) -> None:
        self.urlopen = urlopen or tapi._default_urlopen
        self.accessor = accessor
        self._session: Optional[tuple[str, str]] = None
        self._applicant = ""

    # -- session -----------------------------------------------------------
    def _token_and_base(self) -> tuple[str, str]:
        if self._session is None:
            if not tapi.direct_task_api_enabled():
                raise RuntimeError(f"{tapi.DIRECT_TASK_API_ENV}=0")
            username = tapi.act_as_username()
            if not username or tapi.is_vendor_integration_username(username):
                raise RuntimeError("direct Task API act-as username unset or refused")
            token, app, status = tapi._ensure_token(username, self.accessor, self.urlopen)
            if not token or app is None:
                raise RuntimeError(f"EZLynx login unavailable ({status})")
            self._session = (token, app["discussion_base"])
        return self._session

    def _robie_discussions(self, applicant_id: str) -> list[str]:
        token, base = self._token_and_base()
        url = f"{base}/v8/discussions/by-applicant?" + parse.urlencode({"applicantId": applicant_id})
        listed, err = tapi._get_json(url, token, self.urlopen)
        if err:
            raise RuntimeError(f"discussion list failed ({err})")
        wanted = tapi.ROBIE_TASK_DISCUSSION_TITLE.casefold()
        return [
            tapi.discussion_id_of(r) for r in tapi._discussion_records(listed)
            if tapi.discussion_title_of(r).casefold() == wanted and tapi.discussion_id_of(r)
        ]

    def _notes(self, discussion_id: str) -> list[dict[str, Any]]:
        token, base = self._token_and_base()
        quoted = parse.quote(discussion_id, safe="")
        doc, err = tapi._get_json(f"{base}/v8/discussions/{quoted}/with-notes", token, self.urlopen)
        if err:
            raise RuntimeError(f"note read failed for discussion {discussion_id} ({err})")
        return tapi._records(doc, ("notes", "Notes", "items", "Items"))

    @staticmethod
    def _gate(applicant_id: str, body: str) -> None:
        from .ezlynx_discussions import reject_phone_numbers
        from .ezlynx_write_scope import require_allowed_ezlynx_write_applicant

        require_allowed_ezlynx_write_applicant(applicant_id)
        reject_phone_numbers(body)

    # -- port --------------------------------------------------------------
    def find_for_event(self, event: dict[str, Any]) -> dict[str, Any]:
        key = event["key"]
        applicant = str(event.get("applicant_id") or "")
        self._applicant = applicant
        ids: dict[str, str] = {}
        for discussion_id in self._robie_discussions(applicant):
            for note in self._notes(discussion_id):
                src, component = parse_marker(note.get("body"))
                if src == key and component and component not in ids:
                    ids[component] = tapi.note_id_of(note)
        # Every place this port writes was read without error.
        absent = [c for c in ("note_id", "task_id") if c not in ids]
        return {"source_key": key, "ids": ids, "authoritative_absent": absent,
                "binding": {"applicant_id": applicant}}

    def send_component(self, event: dict[str, Any], key: str, component: str) -> dict[str, Any]:
        applicant = str(event["applicant_id"])
        text = f"{event.get('note_text') or event.get('title') or ''}\n\n{marker(key, component)}".strip()
        if component == "task_id":
            result = tapi.create_task(
                applicant_id=applicant,
                title=str(event.get("title") or "Ascend follow-up"),
                description=f"{event.get('task_text') or ''}\n\n{marker(key, component)}".strip(),
                assignee=str(event.get("assignee") or ""),
                assigned_user_id=event.get("assignee_user_id"),
                due_date=event.get("due_date"),
                urlopen=self.urlopen,
                accessor=self.accessor,
            )
            if result.get("status") != tapi.CREATED:
                return {"ids": {}, "reason": result.get("reason") or result.get("status"),
                        "not_sent": tapi.zapier_fallback_allowed(result)}
            return {"ids": {"task_id": str(result.get("note_id") or "")}}
        if component != "note_id":
            return {"ids": {}, "reason": f"unknown component {component}", "not_sent": True}
        try:
            self._gate(applicant, text)
        except Exception as exc:  # noqa: BLE001 - refused before any request
            return {"ids": {}, "reason": f"{type(exc).__name__}: {exc}", "not_sent": True}
        token, base = self._token_and_base()
        note = {"type": "Note", "body": text}
        discussions = self._robie_discussions(applicant)
        if discussions:
            target = max(discussions, key=lambda d: int(d) if d.isdigit() else 0)
            url = f"{base}/v8/discussions/{parse.quote(target, safe='')}/notes"
            body: dict[str, Any] = note
        else:
            url = f"{base}/v8/discussions/with-note"
            body = {"applicantId": int(applicant),
                    "discussion": {"title": tapi.ROBIE_TASK_DISCUSSION_TITLE}, "note": note}
        req = request.Request(url, data=json.dumps(body).encode("utf-8"),
                              headers=tapi._headers(token, json_body=True), method="POST")
        status, _text, transport = tapi._send(req, self.urlopen, [token])
        # Whatever the response, the receipt is what a fresh read finds.
        found = self.find_for_event(event)
        note_id = found["ids"].get("note_id")
        reason = "" if note_id else (transport or f"HTTP {status}")
        return {"ids": {"note_id": note_id} if note_id else {}, "reason": reason}

    def readback(self, ids: dict[str, str]) -> dict[str, Any]:
        """Find each id among the notes of the applicant's ``Tasks by Robie``
        discussions (listed by applicant), so the applicant binding comes from
        EZLynx itself, not from this process."""
        applicant = self._applicant
        out: dict[str, Any] = {}
        if not applicant:
            return out
        wanted = {str(v): k for k, v in ids.items() if v}
        for discussion_id in self._robie_discussions(applicant):
            for note in self._notes(discussion_id):
                component = wanted.get(tapi.note_id_of(note))
                if not component:
                    continue
                src, marked = parse_marker(note.get("body"))
                if marked != component:
                    continue
                if component == "task_id" and str(note.get("type") or "") != "TaskCreationNote":
                    continue
                out[component] = {"id": tapi.note_id_of(note), "source_key": src,
                                  "applicant_id": applicant, "discussion_id": discussion_id}
        return out


# ---------------------------------------------------------------------------
# QuickBooks Online
# ---------------------------------------------------------------------------


class QBODepositDestination:
    """Commission deposits in QBO, found by source marker in PrivateNote."""

    def __init__(self, qb: Any, *, lookback_days: int = 45) -> None:
        self.qb = qb
        self.lookback_days = lookback_days
        self._payee_type = ""

    def _entity_type(self, entity: dict[str, Any]) -> str:
        """The payee's entity type as QBO records it on the deposit line.

        Uses the line's own ``type``. If QBO omits it, the payee id is read
        as the expected type and accepted only when that record's display
        name equals the name on the deposit line.
        """
        from .ascend_destination_mapping import QBO_PAYEE_TYPES

        raw = str(entity.get("type") or "").strip().casefold()
        if raw:
            return QBO_PAYEE_TYPES.get(raw, "")
        expected = self._payee_type
        if expected not in QBO_PAYEE_TYPES.values() or not entity.get("value"):
            return ""
        try:
            record = self.qb._request("GET", f"{expected.lower()}/{entity['value']}").get(expected) or {}
        except Exception:  # noqa: BLE001
            return ""
        same = (str(record.get("Id")) == str(entity["value"])
                and record.get("DisplayName") and record.get("DisplayName") == entity.get("name"))
        return expected if same else ""

    def _realm(self) -> str:
        return str(getattr(self.qb.config, "realm_id", "") or "")

    def _scan(self, since: str) -> list[dict[str, Any]]:
        """Every Deposit with TxnDate >= since, all pages."""
        rows: list[dict[str, Any]] = []
        start = 1
        while True:
            sql = (f"SELECT * FROM Deposit WHERE TxnDate >= '{since}' "
                   f"STARTPOSITION {start} MAXRESULTS {QBO_PAGE}")
            page = (self.qb._request("GET", "query", query_params={"query": sql}).get("QueryResponse") or {})
            batch = page.get("Deposit") or []
            rows.extend(batch)
            if len(batch) < QBO_PAGE:
                return rows
            start += QBO_PAGE

    def find_for_event(self, event: dict[str, Any]) -> dict[str, Any]:
        key = event["key"]
        txn = str(event.get("txn_date") or "")[:10] or datetime.utcnow().strftime("%Y-%m-%d")
        since = (datetime.strptime(txn, "%Y-%m-%d") - timedelta(days=self.lookback_days)).strftime("%Y-%m-%d")
        ids: dict[str, str] = {}
        for row in self._scan(since):
            src, component = parse_marker(row.get("PrivateNote"))
            if src == key and component == "deposit_id":
                ids["deposit_id"] = str(row.get("Id"))
        self._payee_type = str(event.get("payee_type") or "")
        binding = {f: event.get(f) for f in ("realm_id", "account_id", "income_account_id",
                                             "payee_type", "payee_id", "amount_cents", "currency")}
        if self._realm() != str(event.get("realm_id") or ""):
            binding = {}
        return {"source_key": key, "ids": ids, "binding": binding,
                "authoritative_absent": [] if ids else ["deposit_id"]}

    def send_component(self, event: dict[str, Any], key: str, component: str) -> dict[str, Any]:
        if component != "deposit_id":
            return {"ids": {}, "reason": f"unknown component {component}", "not_sent": True}
        if getattr(self.qb.config, "is_production", True) and os.getenv(QBO_PRODUCTION_WRITES_ENV) != "1":
            return {"ids": {}, "reason": "QBO Production writes are not enabled", "not_sent": True}
        if self._realm() != str(event.get("realm_id") or ""):
            return {"ids": {}, "reason": "QBO realm does not match the mapped realm", "not_sent": True}
        amount = int(event["amount_cents"])
        memo = f"Ascend commission payout {event.get('source_id') or key} {marker(key, component)}"
        payload = {
            "TxnDate": str(event.get("txn_date") or "")[:10] or None,
            "PrivateNote": memo,
            "CurrencyRef": {"value": event["currency"]},
            "DepositToAccountRef": {"value": str(event["account_id"])},
            "Line": [{
                "Amount": round(amount / 100, 2),
                "DetailType": "DepositLineDetail",
                "DepositLineDetail": {
                    "AccountRef": {"value": str(event["income_account_id"])},
                    "Entity": {"value": str(event["payee_id"]), "type": str(event["payee_type"])},
                },
            }],
        }
        payload = {k: v for k, v in payload.items() if v is not None}
        try:
            created = self.qb._request("POST", "deposit", json_body=payload).get("Deposit") or {}
            deposit_id = str(created.get("Id") or "")
        except Exception:  # noqa: BLE001 - outcome unknown; settle by search
            deposit_id = ""
        if not deposit_id:
            deposit_id = self.find_for_event(event)["ids"].get("deposit_id", "")
        return {"ids": {"deposit_id": deposit_id} if deposit_id else {}}

    def readback(self, ids: dict[str, str]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        deposit_id = ids.get("deposit_id")
        if not deposit_id:
            return out
        dep = self.qb._request("GET", f"deposit/{deposit_id}").get("Deposit") or {}
        lines = dep.get("Line") or []
        if str(dep.get("Id")) != str(deposit_id) or len(lines) != 1:
            return out
        detail = lines[0].get("DepositLineDetail") or {}
        src, component = parse_marker(dep.get("PrivateNote"))
        if component != "deposit_id":
            return out
        out["deposit_id"] = {
            "id": str(dep.get("Id")),
            "source_key": src,
            "realm_id": self._realm(),
            "account_id": str((dep.get("DepositToAccountRef") or {}).get("value") or ""),
            "income_account_id": str((detail.get("AccountRef") or {}).get("value") or ""),
            "payee_type": self._entity_type(detail.get("Entity") or {}),
            "payee_id": str((detail.get("Entity") or {}).get("value") or ""),
            "amount_cents": int(round(float(dep.get("TotalAmt") or 0) * 100)),
            "currency": str((dep.get("CurrencyRef") or {}).get("value") or ""),
        }
        return out


def _first(record: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = record.get(key)
        if value not in (None, "") and not isinstance(value, (dict, list, bool)):
            return str(value)
    return ""


class CompositeDestination:
    """Route each event kind to its port."""

    def __init__(self, ezlynx: Any = None, qbo: Any = None) -> None:
        self.ports = {"cancellation": ezlynx, "agreement_signed": ezlynx,
                      "accounting_issue": ezlynx, "commission_payout": qbo}

    def _port(self, event: dict[str, Any]) -> Any:
        port = self.ports.get(event.get("kind"))
        if port is None:
            raise RuntimeError(f"no destination port for {event.get('kind')}")
        return port

    def find_for_event(self, event):
        self._current = self._port(event)
        return self._current.find_for_event(event)

    def send_component(self, event, key, component):
        return self._port(event).send_component(event, key, component)

    def readback(self, ids):
        return self._current.readback(ids)
