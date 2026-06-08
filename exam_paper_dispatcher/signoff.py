from __future__ import annotations

from collections import defaultdict
from typing import Optional

from .models import (
    BatchStatus,
    ExitCode,
    PreCheckReport,
    SignoffReport,
    SignoffRow,
)
from .storage import BatchState, Storage, gen_signoff_id


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
    """从最新签收报告中提取已签收的 (exam_id, room_id, subject) -> signoff_item 映射。"""
    latest = batch.load_latest_signoff_report()
    if not latest:
        return {}
    result = {}
    for it in latest.signoff_items:
        key = (it["exam_id"], it["room_id"], it["subject"])
        result[key] = it
    return result


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

    return {
        "has_signoff": True,
        "count": len(signoff_ids),
        "signoff_ids": signoff_ids,
        "index": batch.load_signoff_index(),
        "status": status,
        "signed_rooms": len(signed_keys),
        "total_expected": total_expected,
        "abnormal_count": abnormal_total,
        "last_imported_at": latest.imported_at if latest else None,
        "last_signoff_id": signoff_ids[-1],
    }
