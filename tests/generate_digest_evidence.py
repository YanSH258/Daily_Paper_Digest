"""Create durable synthetic digest evidence using a temporary DB and mock delivery.

Usage: python tests/generate_digest_evidence.py /absolute/evidence/directory
No real API or existing database is used.
"""
import json
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch

from core.db import Database
from core.notifier import Notifier
from digest.service import DigestService


def generate(destination):
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dpd-evidence-") as tmp:
        db = Database(str(Path(tmp) / "test.db"))
        config = {"output": {"output_dir": str(Path(tmp) / "reports")},
                  "relevance_threshold": 5,
                  "digest": {"daily": {"limit": 10}}}
        titles = ["Machine learning interatomic potential", "Material structure prediction",
                  "Density functional theory pseudopotential", "Language model science",
                  "Organic chemistry", "Materials transport"]
        try:
            ids = db.save_articles_batch([
                {"doi": f"10.example/synthetic-{i}", "title": f"{titles[i % len(titles)]} — example {i}",
                 "journal": "Synthetic Journal", "pub_date": "2026-09-10", "abstract": "Synthetic abstract for verification.",
                 "url": f"https://example.invalid/papers/{i}"}
                for i in range(18)
            ])
            conn = db._conn()
            for i, aid in enumerate(ids):
                conn.execute("UPDATE articles SET relevance=?,created_at=?,evidence_level=?,relevance_reason=? WHERE id=?",
                             (9 - i / 20, "2026-09-10 08:00:00", "ABSTRACT", "Synthetic selection reason", aid))
            conn.commit()
            notifier = Notifier(config)
            svc = DigestService(db, config, notifier=notifier)
            calls = []
            def first_delivery(**kwargs):
                ch = kwargs["channels"][0]
                calls.append(ch)
                if ch == "feishu":
                    raise RuntimeError("simulated definitive rejection")
            with patch.object(notifier, "send_digest_files", side_effect=first_delivery):
                first = svc.publish_digest("2026-09-10", request_key="synthetic-evidence", channels=["email", "feishu"])
            with patch.object(notifier, "send_digest_files", side_effect=lambda **kw: calls.extend(kw["channels"])):
                final = svc.retry_digest_send(first["version_id"], channels=["feishu"])
            assert calls == ["email", "feishu", "feishu"], calls
            assert final["overall_status"] == "success", final
            for artifact in final["artifacts"]:
                shutil.copy2(artifact["path"], destination / Path(artifact["path"]).name)
            snapshot = svc.get_digest(first["version_id"])
            for artifact in snapshot["artifacts"]:
                artifact["path"] = Path(artifact["path"]).name
            (destination / "snapshot.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
            evidence = {"synthetic": True, "input_count": 18, "version_id": first["version_id"],
                        "selected_count": final["selected_count"], "before": first["deliveries"],
                        "after": final["deliveries"], "transport_calls": calls,
                        "before_status": first["overall_status"], "after_status": final["overall_status"]}
            (destination / "delivery-recovery.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
            return evidence
        finally:
            db.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: generate_digest_evidence.py OUTPUT_DIRECTORY")
    print(json.dumps(generate(sys.argv[1]), ensure_ascii=False, indent=2))
