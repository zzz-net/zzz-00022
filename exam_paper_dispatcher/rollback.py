from __future__ import annotations

from pathlib import Path
from typing import Optional

from .models import (
    BatchStatus,
    ExitCode,
    compute_sha256,
)
from .storage import BatchState


class RollbackError(Exception):
    def __init__(self, message: str, exit_code: int, details: Optional[dict] = None):
        super().__init__(message)
        self.exit_code = exit_code
        self.details = details or {}


def rollback_batch(batch: BatchState, force: bool = False) -> tuple[list[dict], Optional[RollbackError]]:
    """回滚批次。遇到目标文件被替换（SHA 不匹配）时停止并说明。

    force=True 时，跳过 SHA 校验强制删除。
    """
    report = batch.load_dispatch_report()
    if not report:
        return [], RollbackError(
            "未找到发放报告，无法回滚",
            ExitCode.BATCH_NOT_FOUND,
        )

    items_data = report.get("items", [])
    if not items_data:
        return [], RollbackError(
            "发放报告中无发放记录",
            ExitCode.BATCH_NOT_FOUND,
        )

    batch.set_status(BatchStatus.ROLLING_BACK, notes=f"开始回滚 {len(items_data)} 项")

    results: list[dict] = []
    stop_reason: Optional[str] = None
    stop_details: Optional[dict] = None

    for item_data in items_data:
        target_path = item_data.get("target_path", "")
        expected_sha = item_data.get("target_sha256", "")
        target_name = item_data.get("room_row", {}).get("target_name", "")
        dispatched = item_data.get("dispatched", False)

        entry = {
            "target_name": target_name,
            "target_path": target_path,
            "action": "skip",
            "reason": "",
        }

        if not dispatched:
            entry["reason"] = "该项目未发放成功"
            results.append(entry)
            continue

        p = Path(target_path)
        if not p.exists():
            entry["action"] = "skip"
            entry["reason"] = "目标文件已不存在"
            results.append(entry)
            continue

        if not force and expected_sha:
            try:
                actual_sha = compute_sha256(p)
            except Exception as e:
                stop_reason = f"读取目标文件失败，停止回滚"
                stop_details = {
                    "target_path": target_path,
                    "target_name": target_name,
                    "error": str(e),
                }
                entry["action"] = "error"
                entry["reason"] = f"读取失败: {e}"
                results.append(entry)
                break

            if actual_sha != expected_sha:
                stop_reason = (
                    f"检测到目标文件被第三方修改，为安全起见停止回滚。"
                    f"如确认无误可使用 --force 强制回滚。"
                )
                stop_details = {
                    "target_path": target_path,
                    "target_name": target_name,
                    "expected_sha256": expected_sha,
                    "actual_sha256": actual_sha,
                }
                entry["action"] = "blocked"
                entry["reason"] = "SHA256 不匹配（文件可能被替换）"
                results.append(entry)
                break

        try:
            p.unlink()
            entry["action"] = "deleted"
            entry["reason"] = "已删除"
        except Exception as e:
            entry["action"] = "error"
            entry["reason"] = f"删除失败: {e}"
            stop_reason = f"删除文件失败，停止回滚"
            stop_details = {
                "target_path": target_path,
                "target_name": target_name,
                "error": str(e),
            }
            results.append(entry)
            break

        results.append(entry)

    batch.save_rollback_report(results)

    if stop_reason:
        batch.set_status(
            BatchStatus.ROLLBACK_FAILED,
            notes=f"回滚中断: {stop_reason}",
        )
        return results, RollbackError(stop_reason, ExitCode.ROLLBACK_TAMPERED, stop_details or {})

    deleted = sum(1 for r in results if r["action"] == "deleted")
    batch.set_status(
        BatchStatus.ROLLED_BACK,
        notes=f"回滚完成: 删除 {deleted}/{len(results)} 项",
    )
    return results, None
