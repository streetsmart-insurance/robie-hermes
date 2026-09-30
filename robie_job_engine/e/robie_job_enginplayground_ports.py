"""Source-backed Playground ports. Reads never call a writer.

Name resolution is deliberately not inferred. Policy lookup needs an explicit
policy number; document/discussion lookup needs an explicit applicant id.
Note callbacks are API-only and additionally limited to Buster Brown.
"""
from __future__ import annotations
import re
from typing import Any
from .playground_config import live_writes_enabled, BUSTER_BROWN_APPLICANT_ID
from .playground_guardrails import Proposal

class PlaygroundPorts:
    def __init__(self, read_port: Any = None, discussion_client: Any = None, note_writer: Any = None):
        self.read_port = read_port
        self.discussion_client = discussion_client
        self.note_writer = note_writer

    def _read_port(self):
        if self.read_port is None:
            from .ezlynx_api_read_port import EzlynxApiClientReadPort
            self.read_port = EzlynxApiClientReadPort()
        return self.read_port

    def _discussions(self):
        if self.discussion_client is None:
            from .ezlynx_api_only_writes import load_discussion_api_config
            from .ezlynx_discussions import DiscussionApiClient
            self.discussion_client = DiscussionApiClient(load_discussion_api_config())
        return self.discussion_client

    def read(self, proposal: Proposal) -> str | None:
        # Current values and note readback require a typed reader, not a list
        # of documents or a writer receipt. Missing support remains missing.
        if proposal.kind != 'lookup':
            return None
        body = str(proposal.body or '')
        match = re.search(r'\bpolicy(?:\s+number)?\s*#?\s*([A-Z0-9][A-Z0-9-]{4,})\b', body, re.I)
        if match:
            number = match.group(1)
            if not any(char.isdigit() for char in number):
                return None
            record = self._read_port().policy_by_number(number)
            from .chat_ezlynx_destination_verifier import _policy_matches
            matches = _policy_matches(record, number, proposal.applicant_id)
            if len(matches) != 1:
                return None
            returned = number
            return f'Found policy {returned} in EZLynx. Coverage details still need the policy document.'
        if not proposal.applicant_id:
            return None
        if not re.search(r'\b(?:documents?|dec page|loss runs)\b', body, re.I):
            return None
        docs = self._read_port().documents_for_applicant(proposal.applicant_id)
        safe = [str(row.get('name') or '').strip() for row in docs if isinstance(row, dict) and str(row.get('id') or '').strip() and row.get('name')]
        if not safe:
            return None
        return 'EZLynx documents for applicant ' + proposal.applicant_id + ': ' + '; '.join(safe[:5]) + '. I have not compared their contents.'

    def discussions(self, proposal: Proposal) -> list[dict[str, Any]]:
        if not proposal.applicant_id:
            return []
        from .ezlynx_discussions import discussion_id_of, discussion_title_of
        try:
            rows = self._discussions().get_discussions(proposal.applicant_id)
        except Exception:
            # No source confirmation is not success, and cannot trigger a note.
            return []
        return [{'id': discussion_id_of(row), 'title': discussion_title_of(row)} for row in rows if isinstance(row, dict) and discussion_id_of(row) and discussion_title_of(row)]

    def file_note(self, proposal: Proposal, title: str, body: str) -> str:
        if proposal.applicant_id != BUSTER_BROWN_APPLICANT_ID or not live_writes_enabled():
            raise PermissionError('Playground note writes are restricted to enabled Buster Brown tests')
        if not title.strip() or not body.strip():
            raise ValueError('An existing discussion title and exact note text are required')
        from .ezlynx_api_only_writes import add_note_to_discussion
        writer = self.note_writer or add_note_to_discussion
        result = writer(proposal.applicant_id, body, discussion_title=title, discussion_client=self._discussions())
        if not isinstance(result, dict) or result.get('status') != 'filed' or result.get('read_back') is not True:
            return ''
        return str(result.get('note_id') or '').strip()

def runtime_ports() -> PlaygroundPorts:
    return PlaygroundPorts()


def chat_port_kwargs(conversation_id: str | None) -> dict[str, Any]:
    from .playground_config import message_in_playground
    if not message_in_playground(conversation_id):
        return {}
    from .playground_sop import load_index
    ports = runtime_ports()
    return {'read': ports.read, 'discussions': ports.discussions,
            'file_note': ports.file_note, 'sop_docs': load_index()}
