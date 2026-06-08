from __future__ import annotations

from collections import defaultdict
from typing import Optional

from .models import (
    BatchStatus,
    ExitCode,
    PreCheckReport,
    SignoffReport,
    SignoffRow,
    SignoffAuditEntry,
    SignoffAuditAction,
    gen_signoff_audit_id,
)
from .storage import BatchState, Storage, gen_signoff_id


CORRECTABLE_FIELDS = {
    "signoff_person",
    "signoff_time",
    "received_count",
    "damage_note",
    "remark",
}


class SignoffError(Exception):
    def __init__(self, message: str, exit_code: int, details: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code
        self.details = details or {}


def _get_batch_rooms(batch: BatchState) -> dict[tuple[str, str, str], int]:
    """从批次的预检报告中提取 (exam_id, room_id, subject) -> students_count 映射。"""
    precheck: Optional[PreCheckReport] = batch.load_precheck_report()
    if not precheck:
        return {}
    result = {}
    for item in precheck.items:
        rr = item.room_row
        key = (rr.exam_id, rr.room_id, rr.subject)
        result[key] = rr.students_count
    return result


def _get_existing_signoffs(batch: BatchState) -> dict[tuple[str, str, str], dict]:
    """从最新签收报告 + 有效状态中提取已签收的（包括已撤销的）。"""
    effective = batch.get_effective_signoff_items()
    if not effective:
        return {}
    return dict(effective)


def import_signoff(
    storage: Storage,
    batch: BatchState,
    signoff_rows: list[SignoffRow],
    csv_path: str,
    force: bool = False,
) -> tuple[SignoffReport, Optional[SignoffError]]:
    """导入签收数据，执行校验，返回报告和错误。

    校验规则：
    1. 批次必须已发放（状态为 completed）
    2. 每个考场 (exam_id, room_id, subject) 必须属于该批次
    3. received_count 必须与预检人数匹配
    4. 同一考场重复导入不能静默覆盖，需 --force 确认
    """
    signoff_id = gen_signoff_id()
    report = SignoffReport(
        signoff_id=signoff_id,
        batch_id=batch.batch_id,
        csv_path=csv_path,
        total_rows=len(signoff_rows),
    )

    if batch.status != BatchStatus.COMPLETED:
        error = SignoffError(
            f"批次 {batch.batch_id} 状态为 {batch.status.value}，必须先完成发放 (completed) 才能签收",
            exit_code=ExitCode.SIGNOFF_BATCH_NOT_DISPATCHED,
            details={"batch_status": batch.status.value},
        )
        return report, error

    batch_rooms = _get_batch_rooms(batch)
    if not batch_rooms:
        error = SignoffError(
            f"批次 {batch.batch_id} 缺少预检报告，无法校验考场归属",
            exit_code=ExitCode.BATCH_NOT_FOUND,
        )
        return report, error

    existing_signoffs = _get_existing_signoffs(batch)

    seen_keys: dict[tuple[str, str, str], int] = {}
    row_groups: dict[tuple[str, str, str], list[SignoffRow]] = defaultdict(list)

    for row in signoff_rows:
        key = (row.exam_id, row.room_id, row.subject)
        row_groups[key].append(row)
        if key not in seen_keys:
            seen_keys[key] = row.line_no

    for key, dup_rows in row_groups.items():
        if len(dup_rows) > 1:
            report.conflicts.append({
                "type": "duplicate_in_csv",
                "exam_id": key[0],
                "room_id": key[1],
                "subject": key[2],
                "lines": [r.line_no for r in dup_rows],
            })

    for row in signoff_rows:
        key = (row.exam_id, row.room_id, row.subject)

        if key in report.invalid_rooms or key in [
            (c["exam_id"], c["room_id"], c["subject"])
            for c in report.conflicts
            if c.get("type") == "room_not_in_batch"
        ]:
            continue

        if key not in batch_rooms:
            already = any(
                c.get("type") == "room_not_in_batch"
                and (c["exam_id"], c["room_id"], c["subject"]) == key
                for c in report.conflicts
            )
            if not already:
                report.invalid_rooms.append({
                    "line_no": row.line_no,
                    "exam_id": row.exam_id,
                    "room_id": row.room_id,
                    "subject": row.subject,
                    "message": f"考场 {row.room_id}/{row.subject} 不属于批次 {batch.batch_id}",
                })
            continue

        expected_count = batch_rooms[key]
        if row.received_count != expected_count:
            report.count_mismatches.append({
                "line_no": row.line_no,
                "exam_id": row.exam_id,
                "room_id": row.room_id,
                "subject": row.subject,
                "expected": expected_count,
                "received": row.received_count,
            })
            continue

        if key in existing_signoffs and not force:
            old = existing_signoffs[key]
            report.conflicts.append({
                "type": "existing_signoff",
                "exam_id": row.exam_id,
                "room_id": row.room_id,
                "subject": row.subject,
                "old_signoff_person": old.get("signoff_person"),
                "old_signoff_time": old.get("signoff_time"),
                "old_received_count": old.get("received_count"),
                "new_signoff_person": row.signoff_person,
                "new_signoff_time": row.signoff_time,
                "new_received_count": row.received_count,
                "message": "该考场已签收，如需覆盖请加 --force",
            })
            continue

        is_abnormal = bool(row.damage_note) or (row.received_count != expected_count)
        report.signoff_items.append({
            "exam_id": row.exam_id,
            "room_id": row.room_id,
            "subject": row.subject,
            "signoff_person": row.signoff_person,
            "signoff_time": row.signoff_time,
            "received_count": row.received_count,
            "expected_count": expected_count,
            "damage_note": row.damage_note,
            "remark": row.remark,
            "line_no": row.line_no,
            "is_abnormal": is_abnormal,
        })

    report.valid_rows = len(report.signoff_items)
    report.signed_rooms = len({(it["exam_id"], it["room_id"], it["subject"]) for it in report.signoff_items})
    report.abnormal_count = sum(1 for it in report.signoff_items if it.get("is_abnormal"))

    has_errors = (
        len(report.invalid_rooms) > 0
        or len(report.count_mismatches) > 0
        or any(c.get("type") in ("duplicate_in_csv", "existing_signoff") for c in report.conflicts)
    )
    report.passed = not has_errors

    error: Optional[SignoffError] = None
    if not report.passed:
        parts = []
        code = ExitCode.SIGNOFF_CONFLICT
        if report.invalid_rooms:
            parts.append(f"考场不在批次中 {len(report.invalid_rooms)} 项")
            code = ExitCode.SIGNOFF_ROOM_NOT_IN_BATCH
        if report.count_mismatches:
            parts.append(f"份数不匹配 {len(report.count_mismatches)} 项")
            code = ExitCode.SIGNOFF_COUNT_MISMATCH
        existing_conflicts = [c for c in report.conflicts if c.get("type") == "existing_signoff"]
        if existing_conflicts:
            parts.append(f"重复签收冲突 {len(existing_conflicts)} 项，需加 --force 确认更新")
            code = ExitCode.SIGNOFF_UPDATE_WITHOUT_FORCE
        dup_in_csv = [c for c in report.conflicts if c.get("type") == "duplicate_in_csv"]
        if dup_in_csv:
            parts.append(f"CSV 内重复考场 {len(dup_in_csv)} 项")
            code = ExitCode.SIGNOFF_CONFLICT
        error = SignoffError(
            "签收导入失败: " + ", ".join(parts),
            exit_code=code,
            details={
                "invalid_rooms": report.invalid_rooms,
                "count_mismatches": report.count_mismatches,
                "conflicts": report.conflicts,
            },
        )
    else:
        if force:
            from datetime import datetime, timezone
            for it in report.signoff_items:
                key = (it["exam_id"], it["room_id"], it["subject"])
                if key in existing_signoffs:
                    old = existing_signoffs[key]
                    version_before = batch.get_room_version(it["exam_id"], it["room_id"], it["subject"])
                    version_after = batch.increment_room_version(it["exam_id"], it["room_id"], it["subject"])
                    audit_entry = SignoffAuditEntry(
                        audit_id=gen_signoff_audit_id(),
                        batch_id=batch.batch_id,
                        exam_id=it["exam_id"],
                        room_id=it["room_id"],
                        subject=it["subject"],
                        action=SignoffAuditAction.IMPORT,
                        operator="signoff_import",
                        reason="force re-import",
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        version_before=version_before,
                        version_after=version_after,
                        old_values={
                            "signoff_person": old.get("signoff_person"),
                            "signoff_time": old.get("signoff_time"),
                            "received_count": old.get("received_count"),
                            "revoked": old.get("revoked", False),
                        },
                        new_values={
                            "signoff_person": it["signoff_person"],
                            "signoff_time": it["signoff_time"],
                            "received_count": it["received_count"],
                            "revoked": False,
                        },
                    )
                    batch.append_signoff_audit(audit_entry)
        batch.save_signoff_report(report)

    return report, error


def build_signoff_summary(batch: BatchState) -> dict:
    """为 query/export 生成签收摘要。"""
    signoff_ids = batch.list_signoff_ids()
    if not signoff_ids:
        return {"has_signoff": False}

    latest = batch.load_latest_signoff_report()
    all_reports = batch.load_all_signoff_reports()

    total_expected = 0
    precheck = batch.load_precheck_report()
    if precheck:
        total_expected = len(precheck.items)

    signed_keys: set[tuple[str, str, str]] = set()
    abnormal_total = 0
    if latest:
        for it in latest.signoff_items:
            key = (it["exam_id"], it["room_id"], it["subject"])
            signed_keys.add(key)
            if it.get("is_abnormal"):
                abnormal_total += 1

    status = "partial" if len(signed_keys) < total_expected else "complete" if total_expected > 0 else "none"

    audit_log = batch.load_signoff_audit_log()
    audit_count = len(audit_log)
    corrected_count = sum(1 for a in audit_log if a.action == SignoffAuditAction.CORRECT)
    revoked_count = sum(1 for a in audit_log if a.action == SignoffAuditAction.REVOKE)

    effective_items = batch.get_effective_signoff_items()
    active_signed_keys: set[tuple[str, str, str]] = set()
    active_abnormal = 0
    for k, v in effective_items.items():
        if not v.get("revoked", False):
            active_signed_keys.add(k)
            if v.get("is_abnormal"):
                active_abnormal += 1

    active_status = (
        "partial" if len(active_signed_keys) < total_expected
        else "complete" if total_expected > 0
        else "none"
    )

    room_versions = batch.load_room_versions()

    return {
        "has_signoff": True,
        "count": len(signoff_ids),
        "signoff_ids": signoff_ids,
        "index": batch.load_signoff_index(),
        "status": active_status,
        "signed_rooms": len(active_signed_keys),
        "total_expected": total_expected,
        "abnormal_count": active_abnormal,
        "last_imported_at": latest.imported_at if latest else None,
        "last_signoff_id": signoff_ids[-1],
        "audit_count": audit_count,
        "corrected_count": corrected_count,
        "revoked_count": revoked_count,
        "room_versions": room_versions,
    }


def correct_signoff(
    batch: BatchState,
    exam_id: str,
    room_id: str,
    subject: str,
    updates: dict,
    operator: str,
    reason: str,
) -> tuple[dict, Optional[SignoffError]]:
    """更正单个考场的签收记录。

    校验规则：
    1. 批次必须已完成发放
    2. 考场必须属于该批次
    3. 考场必须已签收且未被撤销
    4. 更正字段必须是允许的字段
    5. reason 必填
    6. received_count 需与预检人数匹配（除非用户明确提供）
    """
    if batch.status != BatchStatus.COMPLETED:
        return {}, SignoffError(
            f"批次 {batch.batch_id} 状态为 {batch.status.value}，必须先完成发放才能更正签收",
            exit_code=ExitCode.SIGNOFF_BATCH_NOT_DISPATCHED,
            details={"batch_status": batch.status.value},
        )

    if not reason or not reason.strip():
        return {}, SignoffError(
            "更正签收必须提供原因 (--reason)",
            exit_code=ExitCode.SIGNOFF_AUDIT_MISSING_REASON,
        )

    batch_rooms = _get_batch_rooms(batch)
    key = (exam_id, room_id, subject)
    if key not in batch_rooms:
        return {}, SignoffError(
            f"考场 {room_id}/{subject} 不属于批次 {batch.batch_id}",
            exit_code=ExitCode.SIGNOFF_CORRECT_ROOM_NOT_FOUND,
            details={"exam_id": exam_id, "room_id": room_id, "subject": subject},
        )

    effective = batch.get_effective_signoff_items()
    if key not in effective:
        return {}, SignoffError(
            f"考场 {room_id}/{subject} 尚未签收，无法更正",
            exit_code=ExitCode.SIGNOFF_CORRECT_NOT_SIGNED,
            details={"exam_id": exam_id, "room_id": room_id, "subject": subject},
        )

    current = effective[key]
    if current.get("revoked", False):
        return {}, SignoffError(
            f"考场 {room_id}/{subject} 的签收已被撤销，无法更正，请先重新导入",
            exit_code=ExitCode.SIGNOFF_CORRECT_NOT_SIGNED,
            details={"exam_id": exam_id, "room_id": room_id, "subject": subject, "revoked": True},
        )

    invalid_fields = set(updates.keys()) - CORRECTABLE_FIELDS
    if invalid_fields:
        return {}, SignoffError(
            f"更正字段非法: {', '.join(sorted(invalid_fields))}。允许字段: {', '.join(sorted(CORRECTABLE_FIELDS))}",
            exit_code=ExitCode.SIGNOFF_CORRECT_INVALID_FIELD,
            details={"invalid_fields": sorted(invalid_fields), "allowed_fields": sorted(CORRECTABLE_FIELDS)},
        )

    if not updates:
        return {}, SignoffError(
            "更正内容为空，至少指定一个更正字段",
            exit_code=ExitCode.SIGNOFF_CORRECT_INVALID_FIELD,
        )

    if "received_count" in updates:
        try:
            updates["received_count"] = int(updates["received_count"])
        except (ValueError, TypeError):
            return {}, SignoffError(
                f"received_count 必须是整数，收到: {updates['received_count']}",
                exit_code=ExitCode.SIGNOFF_CORRECT_INVALID_FIELD,
            )

    old_values: dict = {}
    new_values: dict = {}
    for field, new_val in updates.items():
        old_val = current.get(field, "")
        if old_val != new_val:
            old_values[field] = old_val
            new_values[field] = new_val

    if not old_values:
        return {
            "changed": False,
            "message": "所有字段值与当前值相同，无需更正",
            "current": current,
        }, None

    version_before = batch.get_room_version(exam_id, room_id, subject)
    version_after = batch.increment_room_version(exam_id, room_id, subject)

    audit_entry = SignoffAuditEntry(
        audit_id=gen_signoff_audit_id(),
        batch_id=batch.batch_id,
        exam_id=exam_id,
        room_id=room_id,
        subject=subject,
        action=SignoffAuditAction.CORRECT,
        operator=operator,
        reason=reason.strip(),
        version_before=version_before,
        version_after=version_after,
        old_values=old_values,
        new_values=new_values,
    )
    batch.append_signoff_audit(audit_entry)
    batch.save()

    updated = dict(current)
    for field, new_val in new_values.items():
        updated[field] = new_val
    updated["version"] = version_after
    updated["last_updated"] = audit_entry.timestamp
    updated["last_operator"] = operator

    return {
        "changed": True,
        "audit_id": audit_entry.audit_id,
        "version_before": version_before,
        "version_after": version_after,
        "old_values": old_values,
        "new_values": new_values,
        "current": updated,
    }, None


def revoke_signoff(
    batch: BatchState,
    exam_id: str,
    room_id: str,
    subject: str,
    operator: str,
    reason: str,
) -> tuple[dict, Optional[SignoffError]]:
    """撤销单个考场的签收记录（不影响批次发放状态）。

    校验规则：
    1. 批次必须已完成发放
    2. 考场必须属于该批次
    3. 考场必须已签收且未被撤销
    4. reason 必填
    """
    if batch.status != BatchStatus.COMPLETED:
        return {}, SignoffError(
            f"批次 {batch.batch_id} 状态为 {batch.status.value}，必须先完成发放才能撤销签收",
            exit_code=ExitCode.SIGNOFF_BATCH_NOT_DISPATCHED,
            details={"batch_status": batch.status.value},
        )

    if not reason or not reason.strip():
        return {}, SignoffError(
            "撤销签收必须提供原因 (--reason)",
            exit_code=ExitCode.SIGNOFF_AUDIT_MISSING_REASON,
        )

    batch_rooms = _get_batch_rooms(batch)
    key = (exam_id, room_id, subject)
    if key not in batch_rooms:
        return {}, SignoffError(
            f"考场 {room_id}/{subject} 不属于批次 {batch.batch_id}",
            exit_code=ExitCode.SIGNOFF_REVOKE_ROOM_NOT_FOUND,
            details={"exam_id": exam_id, "room_id": room_id, "subject": subject},
        )

    effective = batch.get_effective_signoff_items()
    if key not in effective:
        return {}, SignoffError(
            f"考场 {room_id}/{subject} 尚未签收，无法撤销",
            exit_code=ExitCode.SIGNOFF_REVOKE_NOT_SIGNED,
            details={"exam_id": exam_id, "room_id": room_id, "subject": subject},
        )

    current = effective[key]
    if current.get("revoked", False):
        return {}, SignoffError(
            f"考场 {room_id}/{subject} 的签收已经撤销",
            exit_code=ExitCode.SIGNOFF_REVOKE_NOT_SIGNED,
            details={"exam_id": exam_id, "room_id": room_id, "subject": subject, "already_revoked": True},
        )

    version_before = batch.get_room_version(exam_id, room_id, subject)
    version_after = batch.increment_room_version(exam_id, room_id, subject)

    audit_entry = SignoffAuditEntry(
        audit_id=gen_signoff_audit_id(),
        batch_id=batch.batch_id,
        exam_id=exam_id,
        room_id=room_id,
        subject=subject,
        action=SignoffAuditAction.REVOKE,
        operator=operator,
        reason=reason.strip(),
        version_before=version_before,
        version_after=version_after,
        old_values={"signed": True, "revoked": False},
        new_values={"signed": False, "revoked": True},
    )
    batch.append_signoff_audit(audit_entry)
    batch.save()

    return {
        "revoked": True,
        "audit_id": audit_entry.audit_id,
        "version_before": version_before,
        "version_after": version_after,
        "room_id": room_id,
        "subject": subject,
        "exam_id": exam_id,
        "revoked_at": audit_entry.timestamp,
        "revoked_by": operator,
        "revoke_reason": reason.strip(),
    }, None


def get_signoff_history(
    batch: BatchState,
    exam_id: Optional[str] = None,
    room_id: Optional[str] = None,
    subject: Optional[str] = None,
) -> list[dict]:
    """查询签收历史。

    可按 exam_id/room_id/subject 过滤，返回每个考场的完整历史（导入记录 + 审计记录 + 当前状态）。
    """
    effective = batch.get_effective_signoff_items()
    all_keys: set[tuple[str, str, str]] = set(effective.keys())

    audit_log = batch.load_signoff_audit_log()
    for entry in audit_log:
        all_keys.add((entry.exam_id, entry.room_id, entry.subject))

    if exam_id:
        all_keys = {k for k in all_keys if k[0] == exam_id}
    if room_id:
        all_keys = {k for k in all_keys if k[1] == room_id}
    if subject:
        all_keys = {k for k in all_keys if k[2] == subject}

    results = []
    for key in sorted(all_keys):
        e, r, s = key
        results.append(batch.get_signoff_room_history(e, r, s))
    return results
