from app.local_settings import load_local_environment


def test_local_settings_preserves_process_priority(tmp_path, monkeypatch):
    monkeypatch.delenv("DOC_OCR_SKIP_DOTENV", raising=False)
    monkeypatch.delenv("DOC_OCR_DATA_DIR", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "process-key")
    (tmp_path / ".env").write_text(
        'DOC_OCR_DATA_DIR="D:/demo data"\nLLM_API_KEY=file-key\n', encoding="utf-8"
    )
    load_local_environment(tmp_path)
    assert __import__("os").environ["DOC_OCR_DATA_DIR"] == "D:/demo data"
    assert __import__("os").environ["LLM_API_KEY"] == "process-key"


def test_isolated_tests_can_skip_local_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DOC_OCR_SKIP_DOTENV", "1")
    monkeypatch.delenv("DOC_OCR_DATA_DIR", raising=False)
    (tmp_path / ".env").write_text("DOC_OCR_DATA_DIR=D:/should-not-load\n", encoding="utf-8")
    load_local_environment(tmp_path)
    assert "DOC_OCR_DATA_DIR" not in __import__("os").environ
