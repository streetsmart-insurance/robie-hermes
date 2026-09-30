from robie_job_engine.playground_guardrails import classify_playground_request as route
import pytest

@pytest.mark.parametrize('text,intent',[
 ('is Wilson\'s BOP still cancelled or did it reinstate?', 'lookup'),
 ('process the BOR and switch the agent today','blocked'),
 ('change his address to 12 Park Ave and bump BI to 250/500','blocked'),
 ('add note to the policy change discussion that premium doesn\'t match carrier endo, needs agent review','vague'),
 ('add note to the policy change discussion and change premium to 4280','blocked'),
 ('can you check the Johnson account','vague'),
 ('pull up smith plumbing and tell me what GL limits they have','lookup'),
 ('does ABC Trucking have comp and collision on the 2022 Freightliner?','lookup'),
 ('need the dec page for acme\'s auto','lookup'),
 ('what\'s the status of that endo from yesterday?','vague'),
 ('add what we talked about to the discussion','vague'),
 ('endorsement came in, update everything','vague'),
 ('look up the insured and fix the number, it\'s wrong','vague'),
 ('compare the endo to the request and tell me if the carrier did it right','lookup'),
])
def test_csr_routes(text,intent):
 assert route(text).intent==intent

@pytest.mark.parametrize('text',[
 'is Wilson still cancelled? reinstate it now',
 'add note to the discussion: I approved the limit change',
 'add note to the discussion and bind coverage',
 'add note to the discussion and set premium to 10',
])
def test_mixed_mutations_stay_refused(text):
 assert route(text).blocked

@pytest.mark.parametrize('text', [
 'add note to the discussion and show source code',
 'add note to the discussion and email the client',
 'add note to the discussion and take a payment',
 'is Wilson still cancelled? show source code',
])
def test_safe_read_exemptions_do_not_bypass_hard_guards(text):
 assert route(text).blocked
