from __future__ import annotations

import csv
import json
import hashlib
import uuid
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class ExitCode:
    SUCCESS = 0
    CONFIG_ERROR = 1
    INVALID_CSV = 2
    MISSING_SOURCE = 3
    TARGET_CONFLICT = 4
    INVALID_SUBJECT = 5
    INVALID_VERSION = 6
    BATCH_NOT_FOUND = 7
    BATCH_ALREADY_DONE = 8
    ROLLBACK_TAMPERED = 9
    IO_ERROR = 10
    BATCH_ID_CONFLICT = 11
    SIGNOFF_BATCH_NOT_DISPATCHED = 12
    SIGNOFF_ROOM_NOT_IN_BATCH = 13
    SIGNOFF_COUNT_MISMATCH = 14
    SIGNOFF_CONFLICT = 15
    SIGNOFF_UPDATE_WITHOUT_FORCE = 16
    AUDIT_OUTPUT_CONFLICT = 20
    AUDIT_OUTPUT_PERMISSION = 21
    AUDIT_MISSING_REPORT = 22
    AUDIT_INVALID_BATCH_STATUS = 23
    AUDIT_VERIFY_FAILED = 24
    SIGNOFF_CORRECT_INVALID_FIELD = 25
    SIGNOFF_CORRECT_ROOM_NOT_FOUND = 26
    SIGNOFF_CORRECT_NOT_SIGNED = 27
    SIGNOFF_REVOKE_ROOM_NOT_FOUND = 28
    SIGNOFF_REVOKE_NOT_SIGNED = 29
    SIGNOFF_AUDIT_OUTPUT_ERROR = 30
    SIGNOFF_AUDIT_MISSING_REASON = 31
    INCIDENT_CONFLICT = 32
    INCIDENT_NOT_FOUND = 33
    INCIDENT_ALREADY_CLOSED = 34
    INCIDENT_INVALID_FIELD = 35
    INCIDENT_AUDIT_OUTPUT_ERROR = 36
    INCIDENT_BATCH_NOT_DISPATCHED = 37
    UNKNOWN_ERROR = 99


class BatchStatus(str, Enum):
    PENDING = "pending"
    PREVIEW = "preview"
    DRY_RUN_PASSED = "dry_run_passed"
    DISPATCHING = "dispatching"
    COMPLETED = "completed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"
    ROLLBACK_FAILED = "rollback_failed"


class DispatchConfig(BaseModel):
    source_root: str
    output_root: str
    default_subjects: list[str] = Field(default_factory=list)
    subject_versions: dict[str, str] = Field(default_factory=dict)
    package_format: str = "dir"
    naming_pattern: str = "{exam_id}_{room_id}_{subject}"
    storage_dir: str = ".exam_dispatch_state"

    @field_validator("package_format")
    @classmethod
    def validate_package_format(cls, v: str) -> str:
        if v not in ("dir", "zip"):
            raise ValueError("package_format must be 'dir' or 'zip'")
        return v

    @classmethod
    def load(cls, path: str | Path) -> "DispatchConfig":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"配置文件不存在: {p}")
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls(**data)


class RoomRow(BaseModel):
    exam_id: str
    room_id: str
    subject: str
    students_count: int
    source_file: str
    target_name: str
    line_no: int = 0

    @classmethod
    def from_csv(cls, path: str | Path) -> list["RoomRow"]:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"考场清单不存在: {p}")
        rows: list[RoomRow] = []
        with p.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            required = {"exam_id", "room_id", "subject", "students_count", "source_file", "target_name"}
            if not required.issubset(set(reader.fieldnames or [])):
                missing = required - set(reader.fieldnames or [])
                raise ValueError(f"CSV 缺少必要列: {', '.join(missing)}")
            for i, row in enumerate(reader, start=2):
                rows.append(cls(
                    exam_id=row["exam_id"].strip(),
                    room_id=row["room_id"].strip(),
                    subject=row["subject"].strip(),
                    students_count=int(row["students_count"]),
                    source_file=row["source_file"].strip(),
                    target_name=row["target_name"].strip(),
                    line_no=i,
                ))
        return rows


class DispatchItem(BaseModel):
    room_row: RoomRow
    source_path: str
    target_path: str
    source_sha256: Optional[str] = None
    target_sha256: Optional[str] = None
    dispatched: bool = False
    error: Optional[str] = None


class PreCheckReport(BaseModel):
    batch_id: str
    total_rows: int = 0
    valid_rows: int = 0
    missing_sources: list[dict] = Field(default_factory=list)
    target_conflicts: list[dict] = Field(default_factory=list)
    invalid_subjects: list[dict] = Field(default_factory=list)
    invalid_versions: list[dict] = Field(default_factory=list)
    items: list[DispatchItem] = Field(default_factory=list)
    passed: bool = False
    checked_at: str = Field(default_factory=lambda: datetime.now().isoformat())


class PreviewReport(BaseModel):
    preview_id: str
    batch_id: str
    preview_type: str = "import_preview"
    config_snapshot: dict = Field(default_factory=dict)
    csv_path: str = ""
    source_root_resolved: str = ""
    output_root_resolved: str = ""
    total_rows: int = 0
    valid_rows: int = 0
    missing_sources: list[dict] = Field(default_factory=list)
    target_conflicts: list[dict] = Field(default_factory=list)
    invalid_subjects: list[dict] = Field(default_factory=list)
    invalid_versions: list[dict] = Field(default_factory=list)
    preview_items: list[dict] = Field(default_factory=list)
    potential_conflicts: list[dict] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    passed: bool = False
    previewed_at: str = Field(default_factory=lambda: datetime.now().isoformat())


def compute_sha256(path: str | Path, chunk_size: int = 8192) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


class SignoffRow(BaseModel):
    exam_id: str
    room_id: str
    subject: str
    signoff_person: str
    signoff_time: str
    received_count: int
    damage_note: str = ""
    remark: str = ""
    line_no: int = 0

    @classmethod
    def from_csv(cls, path: str | Path) -> list["SignoffRow"]:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"签收清单不存在: {p}")
        rows: list[SignoffRow] = []
        with p.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            required = {"exam_id", "room_id", "subject", "signoff_person",
                        "signoff_time", "received_count"}
            if not required.issubset(set(reader.fieldnames or [])):
                missing = required - set(reader.fieldnames or [])
                raise ValueError(f"签收 CSV 缺少必要列: {', '.join(missing)}")
            for i, row in enumerate(reader, start=2):
                rows.append(cls(
                    exam_id=row["exam_id"].strip(),
                    room_id=row["room_id"].strip(),
                    subject=row["subject"].strip(),
                    signoff_person=row["signoff_person"].strip(),
                    signoff_time=row["signoff_time"].strip(),
                    received_count=int(row["received_count"]),
                    damage_note=row.get("damage_note", "").strip(),
                    remark=row.get("remark", "").strip(),
                    line_no=i,
                ))
        return rows


class SignoffReport(BaseModel):
    signoff_id: str
    batch_id: str
    csv_path: str = ""
    total_rows: int = 0
    valid_rows: int = 0
    invalid_rooms: list[dict] = Field(default_factory=list)
    count_mismatches: list[dict] = Field(default_factory=list)
    conflicts: list[dict] = Field(default_factory=list)
    signoff_items: list[dict] = Field(default_factory=list)
    signed_rooms: int = 0
    abnormal_count: int = 0
    passed: bool = False
    imported_at: str = Field(default_factory=lambda: datetime.now().isoformat())


SIGNOFF_AUDIT_LOG_FILE = "signoff_audit_log.jsonl"
SIGNOFF_ROOM_VERSIONS_FILE = "signoff_room_versions.json"


class SignoffAuditAction(str, Enum):
    CORRECT = "correct"
    REVOKE = "revoke"
    IMPORT = "import"


class SignoffAuditEntry(BaseModel):
    audit_id: str
    batch_id: str
    exam_id: str
    room_id: str
    subject: str
    action: SignoffAuditAction
    operator: str
    reason: str
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())
    version_before: int = 0
    version_after: int = 0
    old_values: dict = Field(default_factory=dict)
    new_values: dict = Field(default_factory=dict)


def gen_signoff_audit_id() -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"signoff-audit-{ts}-{uuid.uuid4().hex[:6]}"


class IncidentStatus(str, Enum):
    OPEN = "open"
    PROCESSING = "processing"
    CLOSED = "closed"


class IncidentType(str, Enum):
    PACKAGE_DAMAGED = "package_damaged"
    WRONG_PACKAGE = "wrong_package"
    ROOM_CHANGE = "room_change"
    OTHER = "other"


INCIDENT_TYPE_LABELS = {
    IncidentType.PACKAGE_DAMAGED: "试卷包损坏",
    IncidentType.WRONG_PACKAGE: "错装/错发",
    IncidentType.ROOM_CHANGE: "临时换考场",
    IncidentType.OTHER: "其他问题",
}


class IncidentHandlingRecord(BaseModel):
    record_id: str
    ticket_id: str
    operator: str
    action: str
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())
    note: str = ""


class IncidentTicket(BaseModel):
    ticket_id: str
    batch_id: str
    exam_id: str
    room_id: str
    subject: str
    incident_type: IncidentType
    description: str
    operator: str
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    status: IncidentStatus = IncidentStatus.OPEN
    closed_at: Optional[str] = None
    closed_by: Optional[str] = None
    close_reason: str = ""
    attachment_paths: list[str] = Field(default_factory=list)
    handling_records: list[IncidentHandlingRecord] = Field(default_factory=list)


INCIDENT_AUDIT_LOG_FILE = "incident_audit_log.jsonl"
INCIDENTS_DIR = "incidents"
INCIDENT_INDEX_FILE = "incidents_index.json"


def gen_incident_id() -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"incident-{ts}-{uuid.uuid4().hex[:6]}"


def gen_incident_handling_id() -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"incident-handling-{ts}-{uuid.uuid4().hex[:6]}"


class IncidentAuditAction(str, Enum):
    CREATE = "create"
    HANDLE = "handle"
    CLOSE = "close"


class IncidentAuditEntry(BaseModel):
    audit_id: str
    batch_id: str
    ticket_id: str
    exam_id: str
    room_id: str
    subject: str
    action: IncidentAuditAction
    operator: str
    detail: str = ""
    status_before: Optional[str] = None
    status_after: Optional[str] = None
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())


def gen_incident_audit_id() -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"incident-audit-{ts}-{uuid.uuid4().hex[:6]}"

