from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Optional

from .signoff import build_signoff_summary
from .incident import build_incident_summary
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

    signoff_summary = build_signoff_summary(batch)
    result["signoff"] = signoff_summary

    audit_log = batch.load_signoff_audit_log()
    if audit_log:
        result["signoff_audit_log"] = [a.model_dump() for a in audit_log]

    effective_signoffs = batch.get_effective_signoff_items()
    if effective_signoffs:
        result["signoff_effective"] = {
            f"{k[0]}:{k[1]}:{k[2]}": v for k, v in effective_signoffs.items()
        }

    incident_summary = build_incident_summary(batch)
    result["incidents"] = incident_summary

    incident_audit_log = batch.load_incident_audit_log()
    if incident_audit_log:
        result["incident_audit_log"] = [a.model_dump() for a in incident_audit_log]

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
        signoff_summary = build_signoff_summary(b)
        d["signoff_count"] = signoff_summary.get("count", 0)
        d["signoff_status"] = signoff_summary.get("status", "none")
        d["signoff_signed_rooms"] = signoff_summary.get("signed_rooms", 0)
        d["signoff_abnormal_count"] = signoff_summary.get("abnormal_count", 0)
        d["signoff_last_imported_at"] = signoff_summary.get("last_imported_at", "")
        d["signoff_audit_count"] = signoff_summary.get("audit_count", 0)
        d["signoff_corrected_count"] = signoff_summary.get("corrected_count", 0)
        d["signoff_revoked_count"] = signoff_summary.get("revoked_count", 0)

        incident_summary = build_incident_summary(b)
        d["incident_count"] = incident_summary.get("count", 0)
        d["incident_open_count"] = incident_summary.get("open_count", 0)
        d["incident_processing_count"] = incident_summary.get("processing_count", 0)
        d["incident_closed_count"] = incident_summary.get("closed_count", 0)
        d["incident_audit_count"] = incident_summary.get("audit_count", 0)

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
        "signoff_count", "signoff_status", "signoff_signed_rooms",
        "signoff_abnormal_count", "signoff_last_imported_at",
        "signoff_audit_count", "signoff_corrected_count", "signoff_revoked_count",
        "incident_count", "incident_open_count", "incident_processing_count", "incident_closed_count", "incident_audit_count",
    ]
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for b in batches:
            row = {k: b.to_dict().get(k, "") for k in fieldnames}
            preview_ids = b.list_preview_ids()
            row["preview_count"] = len(preview_ids)
            row["latest_preview_id"] = preview_ids[-1] if preview_ids else ""
            signoff_summary = build_signoff_summary(b)
            row["signoff_count"] = signoff_summary.get("count", 0)
            row["signoff_status"] = signoff_summary.get("status", "none")
            row["signoff_signed_rooms"] = signoff_summary.get("signed_rooms", 0)
            row["signoff_abnormal_count"] = signoff_summary.get("abnormal_count", 0)
            row["signoff_last_imported_at"] = signoff_summary.get("last_imported_at", "")
            row["signoff_audit_count"] = signoff_summary.get("audit_count", 0)
            row["signoff_corrected_count"] = signoff_summary.get("corrected_count", 0)
            row["signoff_revoked_count"] = signoff_summary.get("revoked_count", 0)
            incident_summary = build_incident_summary(b)
            row["incident_count"] = incident_summary.get("count", 0)
            row["incident_open_count"] = incident_summary.get("open_count", 0)
            row["incident_processing_count"] = incident_summary.get("processing_count", 0)
            row["incident_closed_count"] = incident_summary.get("closed_count", 0)
            row["incident_audit_count"] = incident_summary.get("audit_count", 0)
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

    signoff_map = batch.get_effective_signoff_items()

    fieldnames = [
        "target_name", "exam_id", "room_id", "subject",
        "students_count", "source_path", "target_path",
        "dispatched", "source_sha256", "target_sha256", "error",
        "signed_off", "signoff_revoked", "signoff_person", "signoff_time",
        "received_count", "damage_note", "signoff_remark",
        "signoff_abnormal", "signoff_version",
        "signoff_last_updated", "signoff_last_operator",
        "signoff_revoked_at", "signoff_revoked_by", "signoff_revoke_reason",
    ]
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for it in items:
            rr = it.get("room_row", {})
            key = (rr.get("exam_id", ""), rr.get("room_id", ""), rr.get("subject", ""))
            si = signoff_map.get(key, {})
            is_signed = bool(si) and not si.get("revoked", False)
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
                "signed_off": "True" if is_signed else "False",
                "signoff_revoked": "True" if si.get("revoked") else "False",
                "signoff_person": si.get("signoff_person", ""),
                "signoff_time": si.get("signoff_time", ""),
                "received_count": si.get("received_count", ""),
                "damage_note": si.get("damage_note", ""),
                "signoff_remark": si.get("remark", ""),
                "signoff_abnormal": si.get("is_abnormal", ""),
                "signoff_version": si.get("version", ""),
                "signoff_last_updated": si.get("last_updated", ""),
                "signoff_last_operator": si.get("last_operator", ""),
                "signoff_revoked_at": si.get("revoked_at", ""),
                "signoff_revoked_by": si.get("revoked_by", ""),
                "signoff_revoke_reason": si.get("revoke_reason", ""),
            })
    return out
