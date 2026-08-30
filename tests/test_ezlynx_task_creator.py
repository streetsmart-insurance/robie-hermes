import pytest
from robie_job_engine.ezlynx_task_creator import EZLynxTaskEngine

def test_ezlynx_task_creation_for_direct_rep():
    incident = {
        "name": "Piotr Gusciora",
        "phone": "(973) 652-8939",
        "date": "08/27/2026",
        "time": "11:49 AM",
        "rep_or_queue": "Jackie Arriola",
        "duration": "4m 01s",
    }
    
    payload = EZLynxTaskEngine.generate_task_payload(
        unreturned_incident=incident,
        applicant_id="123456",
        assigned_csr="Lenin Perdomo",
        assigned_producer="Andrea Illanes",
    )
    
    assert payload.applicant_id == "123456"
    assert payload.assigned_user == "Lenin Perdomo"
    assert "🚨 URGENT CALLBACK: Piotr Gusciora" in payload.task_title
    assert "4m 01s" in payload.task_description
    assert payload.priority == "High"

def test_ezlynx_task_creation_for_department_queue():
    incident = {
        "name": "Global Spray Co",
        "phone": "(201) 850-0229",
        "date": "08/28/2026",
        "time": "12:20 PM",
        "rep_or_queue": "Commercial Queue",
        "duration": "1m 44s",
    }
    
    payload = EZLynxTaskEngine.generate_task_payload(
        unreturned_incident=incident,
        applicant_id="789101",
        assigned_csr=None,
        assigned_producer=None,
    )
    
    assert payload.assigned_user == "Zeus Quezada"  # Commercial lead
    assert payload.priority == "High"
