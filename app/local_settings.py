"""Load local settings before deriving paths; never override process variables."""
import os
from pathlib import Path


def load_local_environment(project: Path) -> None:
    if os.getenv("DOC_OCR_SKIP_DOTENV") == "1":
        return
    settings = project / ".env"
    if not settings.is_file():
        return
    for line in settings.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")
