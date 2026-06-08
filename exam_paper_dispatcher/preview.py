from __future__ import annotations

from pathlib import Path
from typing import Optional

from .models import (
    BatchStatus,
    DispatchConfig,
    ExitCode,
    PreviewReport,
    RoomRow,
)
from .precheck import run_precheck
from .storage import BatchState, Storage, gen_preview_id


class PreviewError(Exception):
    def __init__(self, message: str, exit_code: int, details: Optional[dict] = None):
        super().__init__(message)
        self.exit_code = exit_code
        self.details = details or {}


def _resolve_path(base: Path, raw: str) -> Path:
    p = Path(raw)
    if not p.is_absolute():
        p = base / p
    return p.resolve()


def run_import_preview(
    storage: Storage,
    batch: BatchState,
    config: DispatchConfig,
    rows: list[RoomRow],
    csv_path: str | Path,
    config_path: str | Path,
) -> PreviewReport:
    """执行导入预演：复用 run_precheck 做校验，汇总信息但不写出试卷包。

    同一 batch-id 重复预演不会覆盖旧记录，每次生成新的 preview_id。
    """
    preview_id = gen_preview_id()

    config_base = Path(config_path).resolve().parent
    source_root_resolved = _resolve_path(config_base, config.source_root)
    output_root_resolved = _resolve_path(config_base, config.output_root)

    effective_config = config.model_copy()
    effective_config.source_root = str(source_root_resolved)
    effective_config.output_root = str(output_root_resolved)

    precheck_report, precheck_error = run_precheck(effective_config, rows, batch)

    preview_items = []
    for item in precheck_report.items:
        rr = item.room_row
        version = config.subject_versions.get(rr.subject, "")
        preview_items.append({
            "exam_id": rr.exam_id,
            "room_id": rr.room_id,
            "subject": rr.subject,
            "version": version,
            "students_count": rr.students_count,
            "source_file": rr.source_file,
            "source_path_resolved": item.source_path,
            "source_exists": Path(item.source_path).exists(),
            "target_name": rr.target_name,
            "target_path_resolved": item.target_path,
            "target_ext": Path(item.target_path).suffix,
            "target_already_exists": Path(item.target_path).exists(),
            "source_sha256": item.source_sha256,
        })

    potential_conflicts = []
    target_paths = {}
    for pi in preview_items:
        tpath = pi["target_path_resolved"]
        if tpath in target_paths:
            target_paths[tpath].append(pi)
        else:
            target_paths[tpath] = [pi]
    for tpath, items in target_paths.items():
        if len(items) > 1:
            potential_conflicts.append({
                "type": "target_path_duplicate",
                "target_path": tpath,
                "items": [
                    {"room_id": it["room_id"], "subject": it["subject"], "target_name": it["target_name"]}
                    for it in items
                ],
            })
        elif items and items[0]["target_already_exists"]:
            potential_conflicts.append({
                "type": "target_exists_on_disk",
                "target_path": tpath,
                "items": [
                    {"room_id": it["room_id"], "subject": it["subject"], "target_name": it["target_name"]}
                    for it in items
                ],
            })

    warnings = []
    if precheck_report.invalid_versions:
        warnings.append(f"有 {len(precheck_report.invalid_versions)} 项科目未配置版本")
    if precheck_report.missing_sources:
        warnings.append(f"有 {len(precheck_report.missing_sources)} 项缺失源文件")
    if precheck_report.target_conflicts:
        warnings.append(f"有 {len(precheck_report.target_conflicts)} 组目标文件名冲突")
    if precheck_report.invalid_subjects:
        warnings.append(f"有 {len(precheck_report.invalid_subjects)} 项非法科目或人数")
    if potential_conflicts:
        warnings.append(f"检测到 {len(potential_conflicts)} 项潜在输出冲突")

    report = PreviewReport(
        preview_id=preview_id,
        batch_id=batch.batch_id,
        config_snapshot=config.model_dump(),
        csv_path=str(csv_path),
        source_root_resolved=str(source_root_resolved),
        output_root_resolved=str(output_root_resolved),
        total_rows=precheck_report.total_rows,
        valid_rows=precheck_report.valid_rows,
        missing_sources=precheck_report.missing_sources,
        target_conflicts=precheck_report.target_conflicts,
        invalid_subjects=precheck_report.invalid_subjects,
        invalid_versions=precheck_report.invalid_versions,
        preview_items=preview_items,
        potential_conflicts=potential_conflicts,
        warnings=warnings,
        passed=precheck_report.passed and len(potential_conflicts) == 0,
    )

    if batch.status == BatchStatus.PENDING:
        batch.set_status(BatchStatus.PREVIEW, notes=f"首次预演: {preview_id}")

    batch.save_preview_report(report)

    return report
