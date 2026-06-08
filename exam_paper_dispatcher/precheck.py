from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Optional

from .models import (
    BatchStatus,
    DispatchConfig,
    DispatchItem,
    ExitCode,
    PreCheckReport,
    RoomRow,
    compute_sha256,
)
from .storage import BatchState, Storage


class PreCheckError(Exception):
    def __init__(self, message: str, exit_code: int, details: Optional[dict] = None):
        super().__init__(message)
        self.exit_code = exit_code
        self.details = details or {}


def _resolve_version(subject: str, config: DispatchConfig) -> Optional[str]:
    return config.subject_versions.get(subject)


def run_precheck(
    config: DispatchConfig,
    rows: list[RoomRow],
    batch: Optional[BatchState] = None,
) -> tuple[PreCheckReport, Optional[PreCheckError]]:
    """执行预检，返回 (报告, 错误)。错误为 None 表示全部通过。"""

    source_root = Path(config.source_root)
    report = PreCheckReport(
        batch_id=batch.batch_id if batch else "unknown",
        total_rows=len(rows),
    )

    target_name_map: dict[str, list[RoomRow]] = defaultdict(list)
    for row in rows:
        target_name_map[row.target_name].append(row)

    for target_name, dup_rows in target_name_map.items():
        if len(dup_rows) > 1:
            report.target_conflicts.append({
                "target_name": target_name,
                "rows": [
                    {"line_no": r.line_no, "room_id": r.room_id, "exam_id": r.exam_id, "subject": r.subject}
                    for r in dup_rows
                ],
            })

    for row in rows:
        if config.default_subjects and row.subject not in config.default_subjects:
            report.invalid_subjects.append({
                "line_no": row.line_no,
                "subject": row.subject,
                "allowed": config.default_subjects,
            })
            continue

        if config.subject_versions:
            ver = _resolve_version(row.subject, config)
            if ver is None:
                report.invalid_versions.append({
                    "line_no": row.line_no,
                    "subject": row.subject,
                    "message": f"科目 {row.subject} 未在 subject_versions 中配置",
                })
                continue
            if row.students_count <= 0:
                report.invalid_subjects.append({
                    "line_no": row.line_no,
                    "subject": row.subject,
                    "message": f"人数必须大于0, 当前={row.students_count}",
                })
                continue

        src_path = source_root / row.source_file
        if not src_path.exists() or not src_path.is_file():
            report.missing_sources.append({
                "line_no": row.line_no,
                "source_file": row.source_file,
                "resolved_path": str(src_path),
                "room_id": row.room_id,
                "subject": row.subject,
            })
            continue

        ext = src_path.suffix
        if config.package_format == "zip":
            target_fname = row.target_name + ".zip"
        else:
            target_fname = row.target_name + ext
        target_path = str(Path(config.output_root) / target_fname)

        try:
            src_sha = compute_sha256(src_path)
        except Exception as e:
            report.missing_sources.append({
                "line_no": row.line_no,
                "source_file": row.source_file,
                "resolved_path": str(src_path),
                "room_id": row.room_id,
                "subject": row.subject,
                "message": f"读取源文件失败: {e}",
            })
            continue

        item = DispatchItem(
            room_row=row,
            source_path=str(src_path),
            target_path=target_path,
            source_sha256=src_sha,
        )
        report.items.append(item)

    report.valid_rows = len(report.items)
    report.passed = (
        len(report.missing_sources) == 0
        and len(report.target_conflicts) == 0
        and len(report.invalid_subjects) == 0
        and len(report.invalid_versions) == 0
    )

    error: Optional[PreCheckError] = None
    if not report.passed:
        parts = []
        if report.missing_sources:
            parts.append(f"缺失源文件 {len(report.missing_sources)} 项")
            code = ExitCode.MISSING_SOURCE
        if report.target_conflicts:
            parts.append(f"目标名冲突 {len(report.target_conflicts)} 项")
            code = ExitCode.TARGET_CONFLICT
        if report.invalid_subjects:
            parts.append(f"非法科目/人数 {len(report.invalid_subjects)} 项")
            code = ExitCode.INVALID_SUBJECT
        if report.invalid_versions:
            parts.append(f"版本问题 {len(report.invalid_versions)} 项")
            code = ExitCode.INVALID_VERSION
        error = PreCheckError(
            "预检失败: " + ", ".join(parts),
            exit_code=code,
            details={
                "missing_sources": report.missing_sources,
                "target_conflicts": report.target_conflicts,
                "invalid_subjects": report.invalid_subjects,
                "invalid_versions": report.invalid_versions,
            },
        )

    return report, error


def precheck_and_save(
    storage: Storage,
    batch: BatchState,
    config: DispatchConfig,
    rows: list[RoomRow],
    persist: bool = True,
) -> tuple[PreCheckReport, Optional[PreCheckError]]:
    """执行预检，根据 persist 决定是否持久化。
    关键约束：缺失源文件时 dry-run 不能落已完成批次（即 persist=False 时完全不落盘）。
    """
    report, error = run_precheck(config, rows, batch)

    if persist:
        if report.passed:
            batch.save_precheck_report(report)
            batch.set_status(BatchStatus.DRY_RUN_PASSED, notes=f"预检通过: {report.valid_rows}/{report.total_rows} 项")
        else:
            batch.save_precheck_report(report)
            batch.set_status(BatchStatus.FAILED, notes=f"预检失败: {str(error) if error else 'unknown'}")
    else:
        pass

    return report, error
