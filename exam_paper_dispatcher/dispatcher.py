from __future__ import annotations

import shutil
import zipfile
from pathlib import Path
from typing import Optional

from .models import (
    BatchStatus,
    DispatchConfig,
    DispatchItem,
    ExitCode,
    PreCheckReport,
    compute_sha256,
)
from .storage import BatchState


class DispatchError(Exception):
    def __init__(self, message: str, exit_code: int, details: Optional[dict] = None):
        super().__init__(message)
        self.exit_code = exit_code
        self.details = details or {}


def _dispatch_as_dir(item: DispatchItem) -> bool:
    src = Path(item.source_path)
    tgt = Path(item.target_path)
    tgt.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, tgt)
    item.target_sha256 = compute_sha256(tgt)
    item.dispatched = True
    return True


def _dispatch_as_zip(item: DispatchItem) -> bool:
    src = Path(item.source_path)
    tgt = Path(item.target_path)
    tgt.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(tgt, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(src, arcname=src.name)
    item.target_sha256 = compute_sha256(tgt)
    item.dispatched = True
    return True


def dispatch_items(
    config: DispatchConfig,
    items: list[DispatchItem],
    batch: BatchState,
) -> tuple[list[DispatchItem], Optional[DispatchError]]:
    batch.set_status(BatchStatus.DISPATCHING, notes=f"开始发放 {len(items)} 项")

    dispatched: list[DispatchItem] = []
    errors: list[dict] = []

    for item in items:
        try:
            if config.package_format == "zip":
                _dispatch_as_zip(item)
            else:
                _dispatch_as_dir(item)
            dispatched.append(item)
        except Exception as e:
            item.error = str(e)
            item.dispatched = False
            errors.append({
                "target_name": item.room_row.target_name,
                "source_path": item.source_path,
                "target_path": item.target_path,
                "error": str(e),
            })
            dispatched.append(item)

    all_ok = all(it.dispatched for it in dispatched)
    batch.save_dispatch_report(dispatched)

    if all_ok:
        batch.set_status(
            BatchStatus.COMPLETED,
            notes=f"发放完成: 成功 {sum(1 for i in dispatched if i.dispatched)}/{len(dispatched)}",
        )
        return dispatched, None
    else:
        success = sum(1 for i in dispatched if i.dispatched)
        fail = len(dispatched) - success
        batch.set_status(
            BatchStatus.FAILED,
            notes=f"发放部分失败: 成功 {success}, 失败 {fail}",
        )
        return dispatched, DispatchError(
            f"发放失败: {len(errors)} 项出错",
            exit_code=ExitCode.IO_ERROR,
            details={"errors": errors},
        )


def run_dispatch_from_precheck(
    config: DispatchConfig,
    precheck: PreCheckReport,
    batch: BatchState,
) -> tuple[list[DispatchItem], Optional[DispatchError]]:
    if not precheck.passed:
        return [], DispatchError(
            "预检未通过，不能发放",
            exit_code=ExitCode.MISSING_SOURCE,
        )
    return dispatch_items(config, precheck.items, batch)
