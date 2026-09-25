"""All tests import application modules only after installing isolated paths."""
import os
import tempfile


def pytest_configure(config):
    # Avoid shared Windows username-based temp roots with stale ACLs. This
    # directory is created exclusively for this run, never a user data path.
    if not config.option.basetemp:
        config.option.basetemp = tempfile.mkdtemp(prefix='doc_ocr_test_cases_')

os.environ['DOC_OCR_DATA_DIR'] = tempfile.mkdtemp(prefix='doc_ocr_pytest_')
os.environ['DOC_OCR_SKIP_DOTENV'] = '1'
os.environ['LLM_API_KEY'] = ''
os.environ['LLM_BASE_URL'] = 'http://127.0.0.1:9/unreachable'
