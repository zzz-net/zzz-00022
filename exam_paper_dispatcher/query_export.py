from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Optional

from .storage import BatchState, Storage


def query_batch(storage: Storage, batch_id: str) -> Optional[dict]:
    batch = storage.get_batch(batch_id)
    if not batch:
        return None

    result = batch.to_dict()

    preview_index = batch.load_preview_index()
    preview_ids = batch.list_preview_ids()
    if preview_ids:
        previews_summary = []
        for pid in preview_ids:
            rpt = batch.load_preview_report(pid)
            if rpt:
                previews_summary.append({
                    "preview_id": rpt.preview_id,
                    "previewed_at": rpt.previewed_at,
                    "passed": rpt.passed,
                    "csv_path": rpt.csv_path,
                    "source_root_resolved": rpt.source_root_resolved,
                    "output_root_resolved": rpt.output_root_resolved,
                    "total_rows": rpt.total_rows,
                    "valid_rows": rpt.valid_rows,
                    "missing_sources_count": len(rpt.missing_sources),
                    "target_conflicts_count": len(rpt.target_conflicts),
                    "invalid_subjects_count": len(rpt.invalid_subjects),
                    "invalid_versions_count": len(rpt.invalid_versions),
                    "potential_conflicts_count": len(rpt.potential_conflicts),
                    "warnings": rpt.warnings,
                    "preview_items_count": len(rpt.preview_items),
                })
        result["previews"] = {
            "count": len(preview_ids),
            "index": preview_index,
            "summary": previews_summary,
        }

    precheck = batch.load_precheck_report()
    if precheck:
        result["precheck"] = {
            "passed": precheck.passed,
            "total_rows": precheck.total_rows,
            "valid_rows": precheck.valid_rows,
            "missing_sources": precheck.missing_sources,
            "target_conflicts": precheck.target_conflicts,
            "invalid_subjects": precheck.invalid_subjects,
            "invalid_versions": precheck.invalid_versions,
            "checked_at": precheck.checked_at,
        }

    dispatch = batch.load_dispatch_report()
    if dispatch:
        result["dispatch"] = {
            "saved_at": dispatch.get("saved_at"),
            "items_summary": [
                {
                    "target_name": it.get("room_row", {}).get("target_name"),
                    "room_id": it.get("room_row", {}).get("room_id"),
                    "subject": it.get("room_row", {}).get("subject"),
                    "dispatched": it.get("dispatched"),
                    "target_path": it.get("target_path"),
                    "target_sha256": it.get("target_sha256"),
                    "error": it.get("error"),
                }
                for it in dispatch.get("items", [])
            ],
        }

    rollback = batch.load_rollback_report()
    if rollback:
        result["rollback"] = {
            "saved_at": rollback.get("saved_at"),
            "results": rollback.get("results", []),
        }

    return result


def list_batches(storage: Storage, status_filter: Optional[str] = None) -> list[dict]:
    batches = storage.list_batches()
    if status_filter:
        batches = [b for b in batches if b.status.value == status_filter]
    result = []
    for b in batches:
        d = b.to_dict()
        preview_ids = b.list_preview_ids()
        d["preview_count"] = len(preview_ids)
        if preview_ids:
            d["latest_preview_at"] = preview_ids[-1]
        result.append(d)
    return result


def export_to_json(
    storage: Storage,
    output_path: str | Path,
    batch_id: Optional[str] = None,
) -> Path:
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    if batch_id:
        data = query_batch(storage, batch_id)
        if not data:
            raise ValueError(f"批次不存在: {batch_id}")
    else:
        data = {
            "batches": [query_batch(storage, b.batch_id) for b in storage.list_batches()],
            "events_log": storage.get_events_log(),
        }

    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def export_batches_csv(
    storage: Storage,
    output_path: str | Path,
) -> Path:
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    batches = storage.list_batches()
    fieldnames = [
        "batch_id", "status", "created_at", "updated_at",
        "csv_path", "total_items", "success_count", "fail_count", "notes",
        "preview_count", "latest_preview_id",
    ]
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for b in batches:
            row = {k: b.to_dict().get(k, "") for k in fieldnames}
            preview_ids = b.list_preview_ids()
            row["preview_count"] = len(preview_ids)
            row["latest_preview_id"] = preview_ids[-1] if preview_ids else ""
            writer.writerow(row)
    return out


def export_items_csv(
    storage: Storage,
    batch_id: str,
    output_path: str | Path,
) -> Path:
    batch = storage.get_batch(batch_id)
    if not batch:
        raise ValueError(f"批次不存在: {batch_id}")

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    dispatch = batch.load_dispatch_report() or {}
    items = dispatch.get("items", [])

    fieldnames = [
        "target_name", "exam_id", "room_id", "subject",
        "students_count", "source_path", "target_path",
        "dispatched", "source_sha256", "target_sha256", "error",
    ]
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for it in items:
            rr = it.get("room_row", {})
            writer.writerow({
                "target_name": rr.get("target_name", ""),
                "exam_id": rr.get("exam_id", ""),
                "room_id": rr.get("room_id", ""),
                "subject": rr.get("subject", ""),
                "students_count": rr.get("students_count", ""),
                "source_path": it.get("source_path", ""),
                "target_path": it.get("target_path", ""),
                "dispatched": it.get("dispatched", ""),
                "source_sha256": it.get("source_sha256", ""),
                "target_sha256": it.get("target_sha256", ""),
                "error": it.get("error", ""),
            })
    return out
