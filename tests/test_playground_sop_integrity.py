import hashlib,json,os
from pathlib import Path
from unittest.mock import patch
import pytest
from robie_job_engine.playground_sop import ingest_sops,load_index

class Drive:
 def export_text(self,*args):return 'Approved synthetic procedure text.'


def record():
 text='Approved synthetic procedure text.'
 return {'doc_id':'fixture-doc','title':'Fixture SOP','text':text,'sha256':hashlib.sha256(text.encode()).hexdigest()}


def test_atomic_ingest_has_version_digest_and_private_permissions(tmp_path):
 p=tmp_path/'index.json'
 with patch('robie_job_engine.playground_sop.collect_documents',return_value=[{'id':'fixture-doc','name':'Fixture SOP','modifiedTime':'2026-09-30T00:00:00Z'}]):
  result=ingest_sops(Drive(),str(p))
 assert result['schema_version']==1 and result['count']==1
 data=json.loads(p.read_text());assert data['imported_at'] and data['schema_version']==1
 assert load_index(str(p))[0]['doc_id']=='fixture-doc'
 assert os.stat(p).st_mode & 0o777 == 0o600


def test_failed_replace_preserves_previous_index_and_cleans_temp(tmp_path):
 p=tmp_path/'index.json';p.write_text('previous')
 with patch('robie_job_engine.playground_sop.collect_documents',return_value=[]),patch('robie_job_engine.playground_sop.os.replace',side_effect=OSError('fixture failure')):
  with pytest.raises(OSError):ingest_sops(Drive(),str(p))
 assert p.read_text()=='previous' and list(tmp_path.glob('.sop-index-*'))==[]

@pytest.mark.parametrize('mutation',['text','digest','missing_digest','future_schema','missing_id'])
def test_changed_or_invalid_versioned_index_fails_closed(tmp_path,mutation):
 d=record();payload={'schema_version':1,'documents':[d]}
 if mutation=='text':d['text']='Changed'
 if mutation=='digest':d['sha256']='0'*64
 if mutation=='missing_digest':d.pop('sha256')
 if mutation=='missing_id':d.pop('doc_id')
 if mutation=='future_schema':payload['schema_version']=2
 p=tmp_path/'index.json';p.write_text(json.dumps(payload));assert load_index(str(p))==[]


def test_legacy_index_can_load_but_present_digest_still_binding(tmp_path):
 d=record();d.pop('sha256');p=tmp_path/'index.json';p.write_text(json.dumps({'documents':[d]}));assert load_index(str(p))==[d]
 d['sha256']='wrong';p.write_text(json.dumps({'documents':[d]}));assert load_index(str(p))==[]
