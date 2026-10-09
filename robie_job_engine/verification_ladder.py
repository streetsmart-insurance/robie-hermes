"""Offline contract for Gemini-proposes / Jev-verifies / human-in-the-loop.

No model, Bland, EZLynx, or policy-system calls occur here. This module does
not grant an action: the executor must separately enforce owner approval and
read back the authoritative destination after any permitted write.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class Question:
    key: str
    kind: str  # TypeSafe Choice, Score, or Noul (boolean probability)
    instructions: str
    criteria: Mapping[str, str] | None = None


@dataclass(frozen=True)
class Pack:
    name: str
    questions: tuple[Question, ...]
    required_evidence: tuple[str, ...]


# Allowed response shapes are fixed in code. Gemini may propose a pack and
# context, not arbitrary model-generated instructions, thresholds, or actions.
PACKS: Mapping[str, Pack] = {
    'voice_address': Pack('voice_address', (
        Question('address_repeat_back', 'noul', 'Did the agent repeat the entire address, including street, city, state, and ZIP?'),
        Question('explicit_confirmation', 'noul', 'Did the caller explicitly confirm the repeated address as correct?'),
        Question('next_step', 'choice', 'What is the observed conversational next step?', {
            'confirmed': 'Full repeat-back explicitly confirmed',
            'correct_or_repeat': 'Caller corrected or asked to hear it again',
            'human_review': 'The exchange remains ambiguous',
        }),
    ), ('call_id', 'transcript', 'call_status')),
    'quote_review': Pack('quote_review', (
        Question('document_state', 'choice', 'Is the document a quote, a bound policy, or unresolved?', {
            'quote_only': 'Quote or proposal, not proof of binding',
            'bound_evidence': 'Evidence of binding appears in provided source',
            'unknown': 'Insufficient evidence to classify',
        }),
        Question('premium_matches', 'noul', 'Does the quoted premium agree with the independent known premium?'),
    ), ('source_document_id', 'source_document_text', 'known_premium_source_id')),
    'bind_review': Pack('bind_review', (
        Question('bound_state', 'choice', 'Does the evidence indicate an actual bound policy rather than quote-only?', {
            'bound_evidence': 'Binding indicated in source evidence',
            'quote_only': 'Quote or request only',
            'unknown': 'Cannot establish binding',
        }),
    ), ('policy_source_id', 'policy_source_text')),
    'post_call_note': Pack('post_call_note', (
        Question('note_faithful', 'noul', 'Does the filed note accurately describe what the call evidence established, without claiming a conversation, voicemail, or completed action that was not observed?'),
        Question('call_outcome', 'choice', 'Which call outcome is supported by the call evidence?', {
            'human_conversation': 'A human conversation was observed',
            'voicemail_left': 'A voicemail message was actually left',
            'no_contact': 'No confirmed human conversation or voicemail',
            'unknown': 'The evidence is inconclusive',
        }),
    ), ('call_id', 'call_status', 'call_transcript', 'filed_note_source_id',
        'filed_note_text', 'discussion_readback_id')),
    'document_file': Pack('document_file', (
        Question('document_matches_job', 'noul', 'Does this document appear to match the intended policy and filing destination?'),
    ), ('document_source_id', 'filing_target_id', 'destination_readback_id')),
}


def prepare(proposal: Mapping[str, Any], evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a dynamic pack choice; yield a TypeSafe-shaped request or HITL.

    The proposer supplies only pack name and an explanatory reason. Never take
    arbitrary question text, answer labels, actions, or confidence thresholds.
    """
    name = proposal.get('pack')
    if name not in PACKS or set(proposal) - {'pack', 'reason'}:
        return {'route': 'human', 'reason': 'unregistered or malformed pack proposal'}
    pack = PACKS[name]
    missing = [key for key in pack.required_evidence if not evidence.get(key)]
    if missing:
        return {'route': 'human', 'reason': 'missing independent evidence', 'missing': missing}
    questions = {}
    for q in pack.questions:
        spec = {'type': q.kind, 'instructions': q.instructions}
        if q.criteria is not None:
            spec['criteria'] = dict(q.criteria)
        questions[q.key] = spec
    return {'route': 'judge', 'pack': name, 'request': {
        'state': {'evidence': dict(evidence), 'context': str(proposal.get('reason') or '')[:300]},
        'questions': questions,
    }}


def decide(prepared: Mapping[str, Any], answers: Mapping[str, Any], *, threshold: float = 0.85) -> dict[str, Any]:
    """Decision aid only. A pass never authorizes a bind, money, or source write."""
    if prepared.get('route') != 'judge':
        return {'route': 'human', 'reason': prepared.get('reason', 'not prepared')}
    pack = PACKS.get(prepared.get('pack'))
    if not pack or not 0.5 <= threshold <= 1:
        return {'route': 'human', 'reason': 'invalid evaluation configuration'}
    for q in pack.questions:
        answer = answers.get(q.key)
        if not isinstance(answer, Mapping):
            return {'route': 'human', 'reason': f'missing answer: {q.key}'}
        try:
            confidence = float(answer['confidence'])
        except (KeyError, TypeError, ValueError):
            return {'route': 'human', 'reason': f'uncertain answer: {q.key}'}
        if not 0 <= confidence <= 1 or confidence < threshold:
            return {'route': 'human', 'reason': f'uncertain answer: {q.key}'}
        if q.kind == 'choice' and answer.get('choice') not in (q.criteria or {}):
            return {'route': 'human', 'reason': f'out-of-schema answer: {q.key}'}
        if q.kind == 'noul':
            try:
                probability = float(answer['noul'])
            except (KeyError, TypeError, ValueError):
                return {'route': 'human', 'reason': f'uncertain answer: {q.key}'}
            if not 0 <= probability <= 1:
                return {'route': 'human', 'reason': f'out-of-schema answer: {q.key}'}
    # Explicitly conservative examples. A judge is not an independent receipt.
    if pack.name == 'voice_address':
        passed = (answers['address_repeat_back']['noul'] >= threshold and
                  answers['explicit_confirmation']['noul'] >= threshold and
                  answers['next_step']['choice'] == 'confirmed')
    elif pack.name == 'quote_review':
        passed = (answers['document_state']['choice'] == 'quote_only' and
                  answers['premium_matches']['noul'] >= threshold)
    elif pack.name == 'bind_review':
        passed = answers['bound_state']['choice'] == 'bound_evidence'
    elif pack.name == 'post_call_note':
        passed = (answers['note_faithful']['noul'] >= threshold and
                  answers['call_outcome']['choice'] != 'unknown')
    else:
        passed = answers['document_matches_job']['noul'] >= threshold
    return {'route': 'reviewed_check_pass' if passed else 'human',
            'pack': pack.name, 'reason': 'judgment only; no action authority or destination receipt'}


def verify_discussion_destination(*, expected_title: str, observed_title: str,
                                  expected_discussion_id: str, observed_discussion_id: str,
                                  source_id: str) -> dict[str, Any]:
    """Exact destination check on an independent EZLynx discussion readback."""
    if not all((expected_title, observed_title, expected_discussion_id,
                observed_discussion_id, source_id)):
        return {'verified': False, 'reason': 'missing destination readback'}
    matched = (expected_title == observed_title and
               expected_discussion_id == observed_discussion_id)
    return {'verified': matched, 'reason': 'exact destination match' if matched else
            'discussion title or identity mismatch', 'source_id': source_id}
