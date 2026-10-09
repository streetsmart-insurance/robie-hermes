"""Invented calls only; no live number, API key, or client transcript."""
from robie_job_engine.bland_voice_harness import (
    grade_address, score_calls, verify_claims, validate_test_plan, build_test_script,
)


def test_address_mismatch_and_explicit_repeat_back():
    case={'case_id':'SYN-1','expected_address':'68 Haddon Avenue, Gibbsboro, NJ 08026',
          'heard_address':'68 Headon Avenue, Dipsboro, NJ 08026',
          'repeat_back':'68 Headon Avenue, Dipsboro, NJ 08026',
          'confirmation':'yes','audio_reviewed':False}
    result=grade_address(case)
    assert result['exact_match'] is False
    assert result['confirmation_valid'] is False
    assert result['safe_for_write'] is False
    case.update(heard_address=case['expected_address'], repeat_back=case['expected_address'])
    assert grade_address(case)['exact_match'] is True
    assert grade_address(case)['safe_for_write'] is False  # transcript alone is insufficient
    case['audio_reviewed']=True
    assert grade_address(case)['safe_for_write'] is True
    case['confirmation']='okay maybe'
    assert grade_address(case)['confirmation_valid'] is False


def test_script_uses_owned_test_number_and_requires_yes_before_any_update_claim():
    script=build_test_script('SYN-1','68 Haddon Avenue, Gibbsboro, NJ 08026')
    assert 'repeat' in script.lower() and 'yes' in script.lower()
    assert 'updated your information' not in script.lower()
    assert validate_test_plan({'number':'+15550100000','owner_confirmed':False,'case_ids':['SYN-1']}) is False
    assert validate_test_plan({'number':'+15550100000','owner_confirmed':True,'case_ids':['SYN-1']}) is True
    assert validate_test_plan({'number':'+17327038530','owner_confirmed':False,'case_ids':['SYN-1']}) is False


def test_voicemail_detection_no_message_is_failure_not_contact():
    calls=[{'call_id':'SYN-C1','status':'completed','answered_by':'human','human_conversation':True},
           {'call_id':'SYN-C2','status':'completed','answered_by':'voicemail','voicemail_message_confirmed':False},
           {'call_id':'SYN-C3','status':'completed','answered_by':'voicemail','voicemail_message_confirmed':True},
           {'call_id':'SYN-C4','status':'accepted','answered_by':'unknown'},
           {'call_id':'SYN-C5','status':'completed','answered_by':'human','human_conversation':False}]
    result=score_calls(calls)
    assert result['total']==5 and result['human_reached']==1 and result['voicemail_left']==1
    assert result['died_at_voicemail']==1 and result['unconfirmed']==2
    assert result['effective_contact_rate']==0.4


def test_claim_requires_independent_receipt_not_transcript_or_dispatch():
    claimed=[{'call_id':'SYN-C1','action':'ezlynx_address_update','target':'SYN-ACCOUNT',
              'claimed_at':'2026-09-26T10:00:00Z','value':'68 Haddon Avenue, Gibbsboro, NJ 08026'}]
    assert verify_claims(claimed,[])['unverified']==1
    same_call=[{'source':'Bland transcript','call_id':'SYN-C1','target':'SYN-ACCOUNT',
                'action':'ezlynx_address_update','value':claimed[0]['value'],'observed_at':'2026-09-26T10:01:00Z'}]
    assert verify_claims(claimed,same_call)['unverified']==1
    actual=[{'source':'EZLynx readback','source_id':'SYN-A1','target':'SYN-ACCOUNT',
             'action':'ezlynx_address_update','value':claimed[0]['value'],
             'observed_at':'2026-09-26T10:02:00Z'}]
    assert verify_claims(claimed,actual)['verified']==1
    actual[0]['value']='Different address'
    assert verify_claims(claimed,actual)['mismatched']==1


def test_repeated_call_id_rejected_and_unknown_not_contact():
    import pytest
    with pytest.raises(ValueError):
        score_calls([{'call_id':'SYN-A','status':'completed','answered_by':'human','human_conversation':True},
                     {'call_id':'SYN-A','status':'completed','answered_by':'human','human_conversation':True}])
    result=score_calls([{'call_id':'SYN-B','status':'completed','answered_by':'human'}])
    assert result['effective_contact_rate']==0 and result['unconfirmed']==1


def test_write_claim_readback_before_call_or_wrong_target_is_not_proof():
    claim={'call_id':'SYN-C','target':'SYN-A','action':'ezlynx_address_update',
           'claimed_at':'2026-09-26T10:00:00Z','value':'68 Haddon Avenue'}
    before={'source':'EZLynx readback','source_id':'SYN-1','target':'SYN-A',
            'action':'ezlynx_address_update','observed_at':'2026-09-26T09:59:00Z','value':'68 Haddon Avenue'}
    assert verify_claims([claim],[before])['unverified']==1
    before.update(observed_at='2026-09-26T10:01:00Z',target='SYN-B')
    assert verify_claims([claim],[before])['unverified']==1
