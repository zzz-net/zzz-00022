from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import (
    BatchStatus,
    ExitCode,
    IncidentTicket,
    IncidentStatus,
    IncidentType,
    IncidentHandlingRecord,
    INCIDENT_TYPE_LABELS,
    gen_incident_id,
    gen_incident_handling_id,
)
from .storage import BatchState, Storage


class IncidentError(Exception):
    def __init__(self, message: str, exit_code: int, details: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code
        self.details = details or {}


def _validate_batch_room(batch: BatchState, exam_id: str, room_id: str, subject: str) -> tuple[bool, Optional[str]]:
    precheck = batch.load_precheck_report()
    if not precheck:
        return True, None
    for item in precheck.items:
        rr = item.room_row
        if rr.exam_id == exam_id and rr.room_id == room_id and rr.subject == subject:
            return True, None
    return False, f"考场 {room_id}/{subject} (exam_id={exam_id}) 不在批次 {batch.batch_id} 的预检报告中"


def _validate_attachment_paths(paths: list[str]) -> tuple[bool, list[str]]:
    invalid = []
    for p in paths:
        if not p:
            continue
        path = Path(p)
        if not path.exists():
            invalid.append(f"附件路径不存在: {p}")
        elif not path.is_file():
            invalid.append(f"附件路径不是文件: {p}")
    return len(invalid) == 0, invalid


def create_incident(
    storage: Storage,
    batch: BatchState,
    exam_id: str,
    room_id: str,
    subject: str,
    incident_type: IncidentType,
    description: str,
    operator: str,
    attachment_paths: Optional[list[str]] = None,
) -> tuple[IncidentTicket, Optional[IncidentError]]:
    attachment_paths = attachment_paths or []

    if not description.strip():
        return IncidentTicket(
            ticket_id="", batch_id=batch.batch_id,
            exam_id=exam_id, room_id=room_id, subject=subject,
            incident_type=incident_type, description="", operator=operator,
        ), IncidentError(
            "异常说明不能为空",
            exit_code=ExitCode.INCIDENT_INVALID_FIELD,
            details={"field": "description"},
        )

    if not operator.strip():
        return IncidentTicket(
            ticket_id="", batch_id=batch.batch_id,
            exam_id=exam_id, room_id=room_id, subject=subject,
            incident_type=incident_type, description="", operator="",
        ), IncidentError(
            "操作人不能为空",
            exit_code=ExitCode.INCIDENT_INVALID_FIELD,
            details={"field": "operator"},
        )

    valid, invalid = _validate_attachment_paths(attachment_paths)
    if not valid:
        return IncidentTicket(
            ticket_id="", batch_id=batch.batch_id,
            exam_id=exam_id, room_id=room_id, subject=subject,
            incident_type=incident_type, description=description, operator=operator,
        ), IncidentError(
            "附件路径校验失败",
            exit_code=ExitCode.INCIDENT_INVALID_FIELD,
            details={"invalid_paths": invalid},
        )

    existing = batch.find_open_incident_by_room(exam_id, room_id, subject)
    if existing is not None:
        return IncidentTicket(
            ticket_id="", batch_id=batch.batch_id,
            exam_id=exam_id, room_id=room_id, subject=subject,
            incident_type=incident_type, description=description, operator=operator,
        ), IncidentError(
            f"同一考场 ({room_id}/{subject}) 存在未关闭的异常处置单: {existing.ticket_id}",
            exit_code=ExitCode.INCIDENT_CONFLICT,
            details={
                "existing_ticket_id": existing.ticket_id,
                "existing_type": existing.incident_type.value,
                "existing_status": existing.status.value,
                "existing_created_at": existing.created_at,
            },
        )

    ticket_id = gen_incident_id()
    ticket = IncidentTicket(
        ticket_id=ticket_id,
        batch_id=batch.batch_id,
        exam_id=exam_id,
        room_id=room_id,
        subject=subject,
        incident_type=incident_type,
        description=description.strip(),
        operator=operator.strip(),
        attachment_paths=list(attachment_paths),
    )

    try:
        batch.save_incident(ticket)
    except OSError as e:
        return ticket, IncidentError(
            f"保存异常处置单失败: {e}",
            exit_code=ExitCode.IO_ERROR,
            details={"error": str(e)},
        )

    return ticket, None


def handle_incident(
    batch: BatchState,
    ticket_id: str,
    operator: str,
    action: str,
    note: str = "",
    new_status: Optional[IncidentStatus] = None,
) -> tuple[Optional[IncidentTicket], Optional[IncidentError]]:
    ticket = batch.load_incident(ticket_id)
    if ticket is None:
        return None, IncidentError(
            f"异常处置单不存在: {ticket_id}",
            exit_code=ExitCode.INCIDENT_NOT_FOUND,
            details={"ticket_id": ticket_id},
        )

    if ticket.status == IncidentStatus.CLOSED:
        return ticket, IncidentError(
            f"异常处置单已关闭，无法追加处理记录: {ticket_id}",
            exit_code=ExitCode.INCIDENT_ALREADY_CLOSED,
            details={"ticket_id": ticket_id, "closed_at": ticket.closed_at},
        )

    if not operator.strip():
        return ticket, IncidentError(
            "操作人不能为空",
            exit_code=ExitCode.INCIDENT_INVALID_FIELD,
            details={"field": "operator"},
        )

    if not action.strip():
        return ticket, IncidentError(
            "处理动作不能为空",
            exit_code=ExitCode.INCIDENT_INVALID_FIELD,
            details={"field": "action"},
        )

    record = IncidentHandlingRecord(
        record_id=gen_incident_handling_id(),
        ticket_id=ticket_id,
        operator=operator.strip(),
        action=action.strip(),
        note=note.strip(),
    )
    ticket.handling_records.append(record)

    if new_status is not None and new_status != ticket.status:
        ticket.status = new_status

    try:
        batch.save_incident(ticket)
    except OSError as e:
        return ticket, IncidentError(
            f"保存异常处置单失败: {e}",
            exit_code=ExitCode.IO_ERROR,
            details={"error": str(e)},
        )

    return ticket, None


def close_incident(
    batch: BatchState,
    ticket_id: str,
    operator: str,
    close_reason: str = "",
) -> tuple[Optional[IncidentTicket], Optional[IncidentError]]:
    ticket = batch.load_incident(ticket_id)
    if ticket is None:
        return None, IncidentError(
            f"异常处置单不存在: {ticket_id}",
            exit_code=ExitCode.INCIDENT_NOT_FOUND,
            details={"ticket_id": ticket_id},
        )

    if ticket.status == IncidentStatus.CLOSED:
        return ticket, IncidentError(
            f"异常处置单已关闭: {ticket_id}",
            exit_code=ExitCode.INCIDENT_ALREADY_CLOSED,
            details={"ticket_id": ticket_id, "closed_at": ticket.closed_at},
        )

    if not operator.strip():
        return ticket, IncidentError(
            "操作人不能为空",
            exit_code=ExitCode.INCIDENT_INVALID_FIELD,
            details={"field": "operator"},
        )

    ticket.status = IncidentStatus.CLOSED
    ticket.closed_at = datetime.now().isoformat()
    ticket.closed_by = operator.strip()
    ticket.close_reason = close_reason.strip()

    try:
        batch.save_incident(ticket)
    except OSError as e:
        return ticket, IncidentError(
            f"保存异常处置单失败: {e}",
            exit_code=ExitCode.IO_ERROR,
            details={"error": str(e)},
        )

    return ticket, None


def list_incidents(
    batch: BatchState,
    status_filter: Optional[IncidentStatus] = None,
) -> list[IncidentTicket]:
    tickets = batch.load_all_incidents()
    if status_filter:
        tickets = [t for t in tickets if t.status == status_filter]
    return tickets


def get_incident_detail(batch: BatchState, ticket_id: str) -> Optional[dict]:
    ticket = batch.load_incident(ticket_id)
    if ticket is None:
        return None
    return {
        "ticket_id": ticket.ticket_id,
        "batch_id": ticket.batch_id,
        "exam_id": ticket.exam_id,
        "room_id": ticket.room_id,
        "subject": ticket.subject,
        "incident_type": ticket.incident_type.value,
        "incident_type_label": INCIDENT_TYPE_LABELS.get(ticket.incident_type, ticket.incident_type.value),
        "description": ticket.description,
        "operator": ticket.operator,
        "created_at": ticket.created_at,
        "status": ticket.status.value,
        "closed_at": ticket.closed_at,
        "closed_by": ticket.closed_by,
        "close_reason": ticket.close_reason,
        "attachment_paths": ticket.attachment_paths,
        "handling_records": [r.model_dump() for r in ticket.handling_records],
        "handling_count": len(ticket.handling_records),
    }


def build_incident_summary(batch: BatchState) -> dict:
    count = batch.count_incidents()
    tickets = batch.load_all_incidents()
    summary = {
        "count": count["total"],
        "total": count["total"],
        "open_count": count["open"],
        "open": count["open"],
        "processing_count": count["processing"],
        "processing": count["processing"],
        "closed_count": count["closed"],
        "closed": count["closed"],
        "has_incident": count["total"] > 0,
    }
    ticket_items = []
    for t in tickets:
        ticket_items.append({
            "ticket_id": t.ticket_id,
            "created_at": t.created_at,
            "status": t.status.value,
            "exam_id": t.exam_id,
            "room_id": t.room_id,
            "subject": t.subject,
            "incident_type": t.incident_type.value,
            "incident_type_label": INCIDENT_TYPE_LABELS.get(t.incident_type, t.incident_type.value),
            "operator": t.operator,
            "handling_count": len(t.handling_records),
            "closed_at": t.closed_at,
        })
    if tickets:
        summary["tickets"] = ticket_items
        summary["items"] = ticket_items
        index = batch.load_incident_index()
        summary["index"] = index
    else:
        summary["tickets"] = []
        summary["items"] = []
    return summary
