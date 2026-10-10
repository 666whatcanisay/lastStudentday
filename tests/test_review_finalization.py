import json

from fastapi import BackgroundTasks
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import DocumentInfo, DocumentRevision
from app.schemas import DocumentPatchRequest
from app.services import revisions
from app.routers.document import patch_document
from scripts.finalize_legacy_review import build_payload


def test_conservative_payload_keeps_generic_date_and_defaults_currency():
    row = {"suggested_type": "invoice", "revision": "1", "source_date": "2026-01-08",
           "source_summary_amount": "54464.66",
           "source_field_candidates": json.dumps({"total_amount": "57732.54"})}
    payload = build_payload(row)
    assert payload["fields"]["invoice_date"] is None
    assert payload["fields"]["currency"] == "CNY"
    assert payload["fields"]["total_amount"] == "57732.54"


def test_review_cleans_keyless_current_rows_but_preserves_history():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        doc = DocumentInfo(file_name="synthetic.png", file_path="synthetic.png", file_type="image",
                           doc_type="other", status=1, engine_type="rapidocr")
        db.add(doc)
        db.flush()
        revisions.publish_revision(db, doc, change_reason="legacy", doc_type="other",
                                   ocr_raw_text="sample", effective_text="sample", engine_type="rapidocr",
                                   fields=[{"field_key": None, "field_name": "金额", "field_value": "88.00"}])
        db.commit()
        result = patch_document(doc.id, DocumentPatchRequest(expected_revision=1, mark_reviewed=True),
                                BackgroundTasks(), db)
        assert result.code == 200
        assert result.data["current_revision"] == 2
        assert result.data["reviewed"]
        assert all(field["field_key"] for field in result.data["extracts"])
        old = db.query(DocumentRevision).filter_by(doc_id=doc.id, revision=1).one()
        assert json.loads(old.fields_json)[0]["field_key"] is None
    engine.dispose()
