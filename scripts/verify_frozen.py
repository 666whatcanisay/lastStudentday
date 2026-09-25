"""Re-evaluate the shipped synthetic frozen corpus in a fresh copy, offline."""
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
    target = Path(sys.argv[1]).resolve()
    source = ROOT / 'reports/acceptance/frozen_corpus'
    shutil.copytree(source / 'data_root', target)
    os.environ.update(DOC_OCR_DATA_DIR=str(target), DOC_OCR_SKIP_DOTENV='1', LLM_API_KEY='')
    with sqlite3.connect(str(target / 'data/doc_ocr_v2.db')) as conn:
        for doc_id, stored in conn.execute('SELECT id, file_path FROM document_info').fetchall():
            name = str(stored).replace('\\', '/').split('/')[-1]
            file = target / 'uploads' / name
            assert file.is_file(), f'missing frozen original #{doc_id}'
            conn.execute('UPDATE document_info SET file_path=? WHERE id=?', (str(file), doc_id))
    from app.database import init_database, SessionLocal
    from app.services.rag_indexer import index_documents
    from scripts.eval_rag import evaluate, EVAL_MODES
    init_database()
    queries = json.loads((source / 'eval_set_frozen.json').read_text(encoding='utf-8'))['queries']
    baseline = json.loads((ROOT / 'reports/acceptance/baseline_rag.json').read_text(encoding='utf-8'))
    with SessionLocal() as db:
        indexing = index_documents(db)
        assert indexing['failed'] == 0, indexing
        results = [evaluate(db, queries, mode, 5) for mode in EVAL_MODES]
    for result in results:
        old = next(r for r in baseline['results'] if r['mode'] == result['mode'])
        assert result['recall']['@5'] >= old['recall']['@5'], result
        if result['mode'] == 'hybrid+rw':
            assert result['neg_acc'] >= old['neg_acc'], result
    output = {'source': str(source), 'queries': len(queries), 'indexing': indexing,
              'results': results, 'baseline': baseline['results'], 'cloud_calls': 0}
    (ROOT / 'reports/acceptance/final_frozen_compare.json').write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
    print('FROZEN_OK')


if __name__ == '__main__':
    main()
