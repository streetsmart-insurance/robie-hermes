from robie_job_engine.verification_ladder import PACKS, prepare, decide


def test_dynamic_selection_fixed_schema_and_missing_evidence():
    proposal={'pack':'quote_review','reason':'Review quote before bind'}
    evidence={'source_document_id':'SYN-DOC','source_document_text':'Synthetic quote',
              'known_premium_source_id':'SYN-PRICE'}
    selected=prepare(proposal,evidence)
    assert selected['route']=='judge' and selected['request']['questions']['document_state']['type']=='choice'
    assert prepare({'pack':'invented'},evidence)['route']=='human'
    assert prepare({**proposal,'questions':{'evil':'act'}},evidence)['route']=='human'
    assert prepare(proposal,{'source_document_id':'SYN-DOC'})['missing']==['source_document_text','known_premium_source_id']


def test_judge_uncertainty_and_negative_route_human():
    prepared=prepare({'pack':'bind_review'}, {'policy_source_id':'SYN-P','policy_source_text':'Synthetic bound policy'})
    assert decide(prepared, {'bound_state':{'choice':'bound_evidence','confidence':0.92}})['route']=='reviewed_check_pass'
    assert decide(prepared, {'bound_state':{'choice':'bound_evidence','confidence':0.6}})['route']=='human'
    assert decide(prepared, {'bound_state':{'choice':'quote_only','confidence':0.95}})['route']=='human'
    assert decide(prepared, {'bound_state':{'choice':'not_in_schema','confidence':0.99}})['route']=='human'
    assert decide(prepared, {})['route']=='human'


def test_voice_judgment_cannot_prove_write():
    prepared=prepare({'pack':'voice_address'}, {'call_id':'SYN-C','transcript':'synthetic', 'call_status':'completed'})
    answers={'address_repeat_back':{'noul':0.97,'confidence':0.97},
             'explicit_confirmation':{'noul':0.95,'confidence':0.95},
             'next_step':{'choice':'confirmed','confidence':0.91}}
    result=decide(prepared,answers)
    assert result['route']=='reviewed_check_pass'
    assert 'no action authority' in result['reason']
    answers['explicit_confirmation']['noul']=0.22
    assert decide(prepared,answers)['route']=='human'


def test_post_call_discussion_and_note_are_distinct_checks():
    from robie_job_engine.verification_ladder import verify_discussion_destination
    params={'expected_title':'Synthetic Audit','observed_title':'Synthetic Audit',
            'expected_discussion_id':'SYN-D1','observed_discussion_id':'SYN-D1','source_id':'SYN-R1'}
    assert verify_discussion_destination(**params)['verified'] is True
    assert verify_discussion_destination(**{**params,'observed_title':'Other'})['verified'] is False
    evidence={'call_id':'SYN-C','call_status':'completed','call_transcript':'Synthetic test',
              'filed_note_source_id':'SYN-N','filed_note_text':'No contact', 'discussion_readback_id':'SYN-R'}
    prepared=prepare({'pack':'post_call_note'},evidence)
    assert prepared['route']=='judge'
    result=decide(prepared,{'note_faithful':{'noul':0.95,'confidence':0.95},
                            'call_outcome':{'choice':'no_contact','confidence':0.95}})
    assert result['route']=='reviewed_check_pass'
    assert verify_discussion_destination(**{**params,'observed_discussion_id':'SYN-D2'})['verified'] is False
