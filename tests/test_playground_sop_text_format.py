"""Synthetic provider responses only. No live account or Drive calls."""
from unittest.mock import Mock, patch
import pytest
from robie_job_engine.playground_sop import _GoogleDrivePort, ingest_sops


def service_with(data):
 service=Mock()
 service.files.return_value.export.return_value.execute.return_value=data
 service.files.return_value.get_media.return_value.execute.return_value=data
 return service


@pytest.mark.parametrize('mime',['application/pdf','application/vnd.openxmlformats-officedocument.wordprocessingml.document',
 'application/vnd.google-apps.spreadsheet','application/octet-stream','', 'image/png'])
def test_unsupported_formats_fail_before_media_download(mime):
 service=service_with(b'fixture binary')
 with pytest.raises(ValueError,match='Unsupported SOP text format'):
  _GoogleDrivePort(service).export_text('fixture',mime)
 service.files.assert_not_called()


@pytest.mark.parametrize('mime',['text/plain','application/vnd.google-apps.document'])
def test_utf8_text_remains_readable(mime):
 service=service_with('Approved fixture procedure: café.'.encode())
 assert _GoogleDrivePort(service).export_text('fixture',mime)=='Approved fixture procedure: café.'
 if mime=='text/plain':service.files.return_value.get_media.assert_called_once_with(fileId='fixture')
 else:service.files.return_value.export.assert_called_once_with(fileId='fixture',mimeType='text/plain')


@pytest.mark.parametrize('mime',['text/plain','application/vnd.google-apps.document'])
def test_invalid_utf8_does_not_become_replacement_text(mime):
 with pytest.raises(UnicodeDecodeError):_GoogleDrivePort(service_with(b'fixture\xff')).export_text('fixture',mime)


def test_failed_binary_import_preserves_previous_index(tmp_path):
 target=tmp_path/'index.json';target.write_text('previous verified fixture index')
 with patch('robie_job_engine.playground_sop.collect_documents',return_value=[{'id':'fixture','name':'Fixture guide','mimeType':'application/pdf'}]):
  with pytest.raises(ValueError):ingest_sops(_GoogleDrivePort(service_with(b'fixture')),str(target))
 assert target.read_text()=='previous verified fixture index'
 assert list(tmp_path.glob('.sop-index-*'))==[]


@pytest.mark.parametrize('mime',['text/plain','application/vnd.google-apps.document'])
def test_nontext_provider_result_is_rejected(mime):
 with pytest.raises(ValueError,match='not text'):_GoogleDrivePort(service_with({'fixture':'not text'})).export_text('fixture',mime)
