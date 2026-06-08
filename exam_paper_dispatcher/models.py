from __future__ import annotations

import csv
import json
import hashlib
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
    UNKNOWN_ERROR = 99


class BatchStatus(str, Enum):
    PENDING = "pending"
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


def compute_sha256(path: str | Path, chunk_size: int = 8192) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()
