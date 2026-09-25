import json
import threading
import zipfile
from pathlib import Path

import httpx
import pytest

from app.services.backup_gate import WriteGate, BackupInProgress, BackupTimeout
from app.services.runtime_guard import DataDirectoryLock
from scripts.restore_backup import precheck, RestoreError, make_snapshot, recover_pending, _log


def test_directory_lock_blocks_second_server_and_restore(tmp_path):
    with DataDirectoryLock(tmp_path):
        with pytest.raises(RuntimeError):
            DataDirectoryLock(tmp_path).acquire()
    with DataDirectoryLock(tmp_path):
        pass


def test_recovery_of_interrupted_switch(tmp_path):
    (tmp_path / 'data').mkdir()
    (tmp_path / 'uploads').mkdir()
    database = tmp_path / 'data/doc_ocr_v2.db'
    original = tmp_path / 'uploads/test.png'
    database.write_bytes(b'before database')
    original.write_bytes(b'before original')
    snapshot = make_snapshot(tmp_path, 'interrupted_test')
    database.write_bytes(b'half switched database')
    original.unlink()
    _log(tmp_path, status='in_progress', phase='switching', snapshot=str(snapshot))
    with DataDirectoryLock(tmp_path):
        recover_pending(tmp_path)
    assert database.read_bytes() == b'before database'
    assert original.read_bytes() == b'before original'
    assert json.loads((tmp_path / 'data/restore_log.json').read_text())['status'] == 'rolled_back'


@pytest.mark.parametrize('name', ['../outside', '/absolute', 'C:/drive', 'uploads/a:stream',
                                  'uploads/../secret', 'uploads/CON.txt'])
def test_restore_rejects_hostile_paths(tmp_path, name):
    archive = tmp_path / 'bad.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr(name, 'not trusted')
    with pytest.raises(RestoreError):
        precheck(archive)


def test_restore_rejects_manifest_outside_zip(tmp_path):
    archive = tmp_path / 'bad_manifest.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('backup_meta.json', json.dumps({'format_version': 1, 'schema_version': 2}))
        z.writestr('manifest.json', json.dumps({'files': [
            {'path': '../escape', 'size': 0, 'sha256': '0' * 64}]}))
        z.writestr('data/doc_ocr_v2.db', b'')
    with pytest.raises(RestoreError):
        precheck(archive)


def test_backup_waits_for_admitted_nested_writer():
    gate = WriteGate()
    entered, release, backed = threading.Event(), threading.Event(), threading.Event()
    def work():
        with gate.write_section():
            entered.set()
            assert release.wait(3)
            gate.abort_if_closed()
            with gate.write_section():
                pass
    def backup():
        with gate.backup_window(timeout=3):
            backed.set()
    writer = threading.Thread(target=work)
    writer.start()
    assert entered.wait(3)
    maint = threading.Thread(target=backup)
    maint.start()
    import time
    deadline = time.monotonic() + 3
    while not gate.closed and time.monotonic() < deadline:
        time.sleep(.01)
    assert gate.closed and not backed.is_set()
    with pytest.raises(BackupInProgress):
        with gate.write_section():
            pass
    release.set()
    writer.join(3)
    maint.join(3)
    assert backed.is_set() and not gate.closed


def test_partial_schema_never_stamped_head():
    from app.database import _detect_structure
    assert _detect_structure({'document_info': {'id', 'doc_type'}}) == 'unknown'


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-Infinity', 'oops', '1e999'])
def test_invalid_amounts_not_silently_saved(value):
    from app.services.field_registry import normalize_amount, NormalizeError
    from app.services.analytics import parse_amount
    with pytest.raises(NormalizeError):
        normalize_amount(value, strict=True)
    assert parse_amount(value) is None


def test_privacy_llm_error_body_redacted(caplog):
    from app.services.llm_client import LLMClient
    client = LLMClient(api_key='synthetic-only')
    client._http = httpx.Client(base_url='http://mock.local', transport=httpx.MockTransport(
        lambda r: httpx.Response(401, text='SECRET_OR_DOCUMENT_MUST_NOT_LEAK')))
    try:
        result = client.chat([{'role': 'user', 'content': 'synthetic'}])
    finally:
        client.close()
    assert result['error'] == 'http_401'
    assert 'SECRET_OR_DOCUMENT_MUST_NOT_LEAK' not in caplog.text


def test_privacy_llm_timeout_and_no_key():
    from app.services.llm_client import LLMClient
    def timeout(request):
        raise httpx.ReadTimeout('secret request body', request=request)
    client = LLMClient(api_key='synthetic-only')
    client._http = httpx.Client(base_url='http://mock.local', transport=httpx.MockTransport(timeout))
    assert client.chat([])['error'] == 'timeout'
    client.close()
    assert LLMClient(api_key='').chat([])['error'] == 'llm_not_configured'


def test_privacy_local_routes_never_call_cloud(monkeypatch):
    from app.main import app
    from app.services.llm_client import LLMClient
    from app.services import ocr_engine, rag_indexer, embedding
    calls = []
    def forbidden(*args, **kwargs):
        calls.append('cloud')
        raise AssertionError('unexpected cloud invocation')
    monkeypatch.setattr(LLMClient, 'chat', forbidden)
    monkeypatch.setattr(ocr_engine, 'get_ocr_engine', lambda: ('mock', None))
    monkeypatch.setattr(ocr_engine, 'get_engine_info', lambda: {'name': 'test', 'engine_type': 'mock'})
    monkeypatch.setattr(rag_indexer, 'compensate_on_startup', lambda: None)
    monkeypatch.setattr(embedding.get_embedding_model(), 'load', lambda: False)
    from fastapi.testclient import TestClient
    with TestClient(app, base_url='http://127.0.0.1') as client:
        for path in ('/api/health', '/api/document/list', '/api/document/analytics',
                     '/api/maintenance/restore-info', '/api/rag/config'):
            assert client.get(path).status_code == 200
        assert client.post('/api/rag/rewrite', json={'query': '合计金额', 'use_llm_rewrite': True}).status_code == 400
        result = client.post('/api/rag/ask', json={'question': '资料不存在', 'doc_ids': []})
        assert result.json()['data']['llm']['called'] is False
        assert client.post('/api/file/upload', headers={'Origin': 'https://evil.example'}).status_code == 403
        assert client.post('/api/file/upload', headers={'Origin': 'http://localhost:9999'}).status_code == 403
        assert client.get('/api/health', headers={'Host': 'evil.example'}).status_code == 403
    assert calls == []


def test_privacy_fabricated_citations_not_accepted():
    from app.services.rag_pipeline import _parse_citations
    assert _parse_citations('事实[1]编造[99]', 1) == [1]
