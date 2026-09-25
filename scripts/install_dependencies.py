"""Install the pinned local runtime without installing two conflicting cv2 wheels."""
import subprocess
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parent.parent
if __name__ == '__main__':
    lines = (root / 'requirements.txt').read_text(encoding='utf-8').splitlines()
    rapid = [line.strip() for line in lines if line.strip().startswith('rapidocr-onnxruntime==')]
    if len(rapid) != 1:
        raise SystemExit('Expected exactly one pinned RapidOCR requirement')
    with tempfile.TemporaryDirectory(prefix='doc_ocr_install_') as directory:
        req = Path(directory) / 'requirements.txt'
        req.write_text('\n'.join(line for line in lines if line.strip() not in rapid), encoding='utf-8')
        subprocess.run([sys.executable, '-m', 'pip', 'install', '-r', str(req)], check=True)
        subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-deps', rapid[0]], check=True)
    # Fail installation immediately if a binary wheel or an explicitly listed
    # RapidOCR runtime dependency cannot actually be imported on this machine.
    subprocess.run([sys.executable, '-c', (
        "import cv2,numpy,onnxruntime,fitz,openpyxl,tokenizers,pyclipper,"
        "shapely,yaml,six,fastapi,sqlalchemy,alembic; "
        "from rapidocr_onnxruntime import RapidOCR; "
        "print('运行时依赖自检通过:', cv2.__version__, onnxruntime.__version__)"
    )], check=True)
