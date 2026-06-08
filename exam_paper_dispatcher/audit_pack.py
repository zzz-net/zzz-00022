from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import (
    BatchStatus,
    ExitCode,
    SIGNOFF_AUDIT_LOG_FILE,
    SIGNOFF_ROOM_VERSIONS_FILE,
    compute_sha256,
)
from .signoff import build_signoff_summary
from .incident import build_incident_summary
from .storage import (
    CONFIG_SNAPSHOT_FILE,
    DISPATCH_REPORT_FILE,
    EVENTS_LOG_FILE,
    PRECHECK_REPORT_FILE,
    ROLLBACK_REPORT_FILE,
    SIGNOFF_INDEX_FILE,
    SIGNOFFS_DIR,
    INCIDENTS_DIR,
    INCIDENT_INDEX_FILE,
    BatchState,
    Storage,
)

MANIFEST_FILE = "manifest.json"
README_FILE = "README.txt"

AUDIT_CONTENTS = [
    CONFIG_SNAPSHOT_FILE,
    PRECHECK_REPORT_FILE,
    DISPATCH_REPORT_FILE,
    ROLLBACK_REPORT_FILE,
    "batch_events.log",
    README_FILE,
    MANIFEST_FILE,
]

SIGNOFF_SUMMARY_FILE = "signoff_summary.json"
SIGNOFF_INDEX_EXPORT = "signoffs_index.json"
SIGNOFF_AUDIT_EXPORT = "signoff_audit_log.jsonl"
SIGNOFF_VERSIONS_EXPORT = "signoff_room_versions.json"

INCIDENT_SUMMARY_FILE = "incident_summary.json"
INCIDENT_INDEX_EXPORT = "incidents_index.json"
INCIDENT_AUDIT_EXPORT = "incident_audit_log.jsonl"


class AuditPackError(Exception):
    def __init__(self, message: str, exit_code: int, details: Optional[dict] = None):
        super().__init__(message)
        self.exit_code = exit_code
        self.details = details or {}


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _dict_digest(d: dict) -> str:
    canonical = json.dumps(d, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return _sha256_bytes(canonical.encode("utf-8"))


def _collect_batch_payload(batch: BatchState, storage: Storage) -> dict[str, bytes]:
    payload: dict[str, bytes] = {}

    snap = batch.load_config_snapshot()
    if snap:
        payload[CONFIG_SNAPSHOT_FILE] = json.dumps(
            snap, ensure_ascii=False, indent=2
        ).encode("utf-8")

    precheck = batch.load_precheck_report()
    if precheck:
        payload[PRECHECK_REPORT_FILE] = json.dumps(
            precheck.model_dump(mode="json"), ensure_ascii=False, indent=2
        ).encode("utf-8")

    dispatch = batch.load_dispatch_report()
    if dispatch:
        payload[DISPATCH_REPORT_FILE] = json.dumps(
            dispatch, ensure_ascii=False, indent=2
        ).encode("utf-8")

    rollback = batch.load_rollback_report()
    if rollback:
        payload[ROLLBACK_REPORT_FILE] = json.dumps(
            rollback, ensure_ascii=False, indent=2
        ).encode("utf-8")

    events = storage.get_events_for_batch(batch.batch_id)
    if events:
        payload["batch_events.log"] = events.encode("utf-8")

    signoff_summary = build_signoff_summary(batch)
    if signoff_summary.get("has_signoff"):
        payload[SIGNOFF_SUMMARY_FILE] = json.dumps(
            signoff_summary, ensure_ascii=False, indent=2
        ).encode("utf-8")

        signoff_index = batch.load_signoff_index()
        if signoff_index:
            payload[SIGNOFF_INDEX_EXPORT] = json.dumps(
                signoff_index, ensure_ascii=False, indent=2
            ).encode("utf-8")

        signoff_ids = batch.list_signoff_ids()
        for sid in signoff_ids:
            rpt = batch.load_signoff_report(sid)
            if rpt:
                fname = f"{SIGNOFFS_DIR}/{sid}.json"
                payload[fname] = json.dumps(
                    rpt.model_dump(mode="json"), ensure_ascii=False, indent=2
                ).encode("utf-8")

        audit_log = batch.load_signoff_audit_log()
        if audit_log:
            lines = []
            for entry in audit_log:
                lines.append(json.dumps(entry.model_dump(mode="json"), ensure_ascii=False))
            payload[SIGNOFF_AUDIT_EXPORT] = ("\n".join(lines) + "\n").encode("utf-8")

        room_versions = batch.load_room_versions()
        if room_versions:
            payload[SIGNOFF_VERSIONS_EXPORT] = json.dumps(
                room_versions, ensure_ascii=False, indent=2
            ).encode("utf-8")

    incident_summary = build_incident_summary(batch)
    if incident_summary.get("has_incident"):
        payload[INCIDENT_SUMMARY_FILE] = json.dumps(
            incident_summary, ensure_ascii=False, indent=2
        ).encode("utf-8")

        incident_index = batch.load_incident_index()
        if incident_index:
            payload[INCIDENT_INDEX_EXPORT] = json.dumps(
                incident_index, ensure_ascii=False, indent=2
            ).encode("utf-8")

        incident_ids = batch.list_incident_ids()
        for tid in incident_ids:
            tkt = batch.load_incident(tid)
            if tkt:
                fname = f"{INCIDENTS_DIR}/{tid}.json"
                payload[fname] = json.dumps(
                    tkt.model_dump(mode="json"), ensure_ascii=False, indent=2
                ).encode("utf-8")

        incident_audit_log = batch.load_incident_audit_log()
        if incident_audit_log:
            lines = []
            for entry in incident_audit_log:
                lines.append(json.dumps(entry.model_dump(mode="json"), ensure_ascii=False))
            payload[INCIDENT_AUDIT_EXPORT] = ("\n".join(lines) + "\n").encode("utf-8")

    return payload


def _build_readme(batch: BatchState, payload: dict[str, bytes]) -> str:
    lines = []
    lines.append("=" * 60)
    lines.append("考务交接审计包 / Exam Dispatch Audit Pack")
    lines.append("=" * 60)
    lines.append("")
    lines.append(f"批次 ID        : {batch.batch_id}")
    lines.append(f"当前状态       : {batch.status.value}")
    lines.append(f"创建时间       : {batch.created_at}")
    lines.append(f"最后更新时间   : {batch.updated_at}")
    if batch.notes:
        lines.append(f"备注           : {batch.notes}")
    lines.append("")
    lines.append(f"项目总数       : {batch.total_items}")
    lines.append(f"发放成功       : {batch.success_count}")
    lines.append(f"发放失败       : {batch.fail_count}")
    lines.append("")

    if SIGNOFF_SUMMARY_FILE in payload:
        signoff = json.loads(payload[SIGNOFF_SUMMARY_FILE].decode("utf-8"))
        lines.append(f"签收状态       : {signoff.get('status', 'none')}")
        lines.append(f"签收导入次数   : {signoff.get('count', 0)}")
        lines.append(f"已签收考场     : {signoff.get('signed_rooms', 0)}/{signoff.get('total_expected', 0)}")
        lines.append(f"签收异常数     : {signoff.get('abnormal_count', 0)}")
        lines.append(f"签收更正次数   : {signoff.get('corrected_count', 0)}")
        lines.append(f"签收撤销次数   : {signoff.get('revoked_count', 0)}")
        lines.append(f"审计日志总数   : {signoff.get('audit_count', 0)}")
        if signoff.get("last_imported_at"):
            lines.append(f"最后签收导入   : {signoff.get('last_imported_at')}")
        lines.append("")

    if INCIDENT_SUMMARY_FILE in payload:
        incident = json.loads(payload[INCIDENT_SUMMARY_FILE].decode("utf-8"))
        lines.append(f"异常处置单总数 : {incident.get('count', 0)}")
        lines.append(f"  未处理(open)  : {incident.get('open_count', 0)}")
        lines.append(f"  处理中        : {incident.get('processing_count', 0)}")
        lines.append(f"  已关闭        : {incident.get('closed_count', 0)}")
        lines.append(f"  审计日志总数  : {incident.get('audit_count', 0)}")
        lines.append("")

    lines.append("包含文件:")
    for name in sorted(payload.keys()):
        size = len(payload[name])
        lines.append(f"  - {name} ({size} bytes)")
    lines.append("")
    lines.append("状态流转说明:")
    status_desc = {
        "pending": "已创建但尚未预检",
        "dry_run_passed": "预检通过，等待发放",
        "dispatching": "发放中",
        "completed": "发放完成",
        "rolling_back": "回滚中",
        "rolled_back": "回滚完成",
        "failed": "预检或发放失败",
        "rollback_failed": "回滚失败（可能检测到篡改）",
    }
    lines.append(f"  {batch.status.value}: {status_desc.get(batch.status.value, '未知')}")
    lines.append("")
    lines.append(f"生成时间       : {datetime.now().isoformat()}")
    lines.append("")
    lines.append("使用说明:")
    lines.append("  1. 用 audit-verify 命令校验本归档完整性和批次一致性")
    lines.append("  2. 如需恢复数据，可将归档中的 JSON 文件导入 storage 目录对应批次子目录")
    lines.append("=" * 60)
    return "\n".join(lines) + "\n"


def _build_manifest(batch: BatchState, payload: dict[str, bytes]) -> dict:
    files_sha: dict[str, str] = {}
    for name, data in payload.items():
        files_sha[name] = _sha256_bytes(data)

    config_digest = ""
    if CONFIG_SNAPSHOT_FILE in payload:
        snap = json.loads(payload[CONFIG_SNAPSHOT_FILE].decode("utf-8"))
        config_digest = _dict_digest(snap.get("config", {}))

    precheck_count = 0
    if PRECHECK_REPORT_FILE in payload:
        rep = json.loads(payload[PRECHECK_REPORT_FILE].decode("utf-8"))
        precheck_count = len(rep.get("items", []))

    dispatch_count = 0
    if DISPATCH_REPORT_FILE in payload:
        rep = json.loads(payload[DISPATCH_REPORT_FILE].decode("utf-8"))
        dispatch_count = len(rep.get("items", []))

    rollback_count = 0
    if ROLLBACK_REPORT_FILE in payload:
        rep = json.loads(payload[ROLLBACK_REPORT_FILE].decode("utf-8"))
        rollback_count = len(rep.get("results", []))

    signoff_count = 0
    signoff_signed_rooms = 0
    signoff_abnormal = 0
    signoff_corrected = 0
    signoff_revoked = 0
    signoff_audit_total = 0
    if SIGNOFF_SUMMARY_FILE in payload:
        summary = json.loads(payload[SIGNOFF_SUMMARY_FILE].decode("utf-8"))
        signoff_count = summary.get("count", 0)
        signoff_signed_rooms = summary.get("signed_rooms", 0)
        signoff_abnormal = summary.get("abnormal_count", 0)
        signoff_corrected = summary.get("corrected_count", 0)
        signoff_revoked = summary.get("revoked_count", 0)
        signoff_audit_total = summary.get("audit_count", 0)

    incident_count = 0
    incident_open = 0
    incident_processing = 0
    incident_closed = 0
    incident_audit_total = 0
    if INCIDENT_SUMMARY_FILE in payload:
        summary = json.loads(payload[INCIDENT_SUMMARY_FILE].decode("utf-8"))
        incident_count = summary.get("count", 0)
        incident_open = summary.get("open_count", 0)
        incident_processing = summary.get("processing_count", 0)
        incident_closed = summary.get("closed_count", 0)
        incident_audit_total = summary.get("audit_count", 0)

    return {
        "schema_version": "1.0",
        "generated_at": datetime.now().isoformat(),
        "batch_id": batch.batch_id,
        "batch_status": batch.status.value,
        "config_digest_sha256": config_digest,
        "counts": {
            "precheck_items": precheck_count,
            "dispatch_items": dispatch_count,
            "rollback_results": rollback_count,
            "signoff_imports": signoff_count,
            "signoff_signed_rooms": signoff_signed_rooms,
            "signoff_abnormal": signoff_abnormal,
            "signoff_corrected": signoff_corrected,
            "signoff_revoked": signoff_revoked,
            "signoff_audit_total": signoff_audit_total,
            "incident_total": incident_count,
            "incident_open": incident_open,
            "incident_processing": incident_processing,
            "incident_closed": incident_closed,
            "incident_audit_total": incident_audit_total,
        },
        "files_sha256": files_sha,
    }


def _validate_batch_before_pack(batch: BatchState) -> Optional[AuditPackError]:
    if batch.status == BatchStatus.PENDING:
        return AuditPackError(
            f"批次 {batch.batch_id} 处于 pending 状态，尚无任何报告可打包",
            ExitCode.AUDIT_INVALID_BATCH_STATUS,
            {"batch_id": batch.batch_id, "status": batch.status.value},
        )

    precheck = batch.load_precheck_report()
    if not precheck:
        return AuditPackError(
            f"批次 {batch.batch_id} 缺少预检报告，无法生成审计包",
            ExitCode.AUDIT_MISSING_REPORT,
            {"batch_id": batch.batch_id, "missing": PRECHECK_REPORT_FILE},
        )

    snap = batch.load_config_snapshot()
    if not snap:
        return AuditPackError(
            f"批次 {batch.batch_id} 缺少配置快照，无法生成审计包",
            ExitCode.AUDIT_MISSING_REPORT,
            {"batch_id": batch.batch_id, "missing": CONFIG_SNAPSHOT_FILE},
        )

    return None


def _check_output_path(output_path: Path, force: bool = False) -> Optional[AuditPackError]:
    parent = output_path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except (PermissionError, OSError) as e:
        return AuditPackError(
            f"无法创建输出目录 {parent}: {e}",
            ExitCode.AUDIT_OUTPUT_PERMISSION,
            {"output_dir": str(parent), "error": str(e)},
        )

    if output_path.exists() and not force:
        return AuditPackError(
            f"输出文件已存在: {output_path}。如需覆盖请指定 --force",
            ExitCode.AUDIT_OUTPUT_CONFLICT,
            {"output_path": str(output_path)},
        )

    test_file = parent / f".audit_write_test_{os.getpid()}.tmp"
    try:
        test_file.write_bytes(b"test")
        test_file.unlink()
    except (PermissionError, OSError) as e:
        return AuditPackError(
            f"输出目录不可写 {parent}: {e}",
            ExitCode.AUDIT_OUTPUT_PERMISSION,
            {"output_dir": str(parent), "error": str(e)},
        )

    return None


def build_audit_pack(
    storage: Storage,
    batch_id: str,
    output_path: str | Path,
    force: bool = False,
) -> tuple[Path, Optional[AuditPackError]]:
    output = Path(output_path)

    batch = storage.get_batch(batch_id)
    if not batch:
        return output, AuditPackError(
            f"批次不存在: {batch_id}",
            ExitCode.BATCH_NOT_FOUND,
            {"batch_id": batch_id},
        )

    err = _validate_batch_before_pack(batch)
    if err:
        return output, err

    err = _check_output_path(output, force=force)
    if err:
        return output, err

    payload = _collect_batch_payload(batch, storage)

    readme = _build_readme(batch, payload)
    payload[README_FILE] = readme.encode("utf-8")

    manifest = _build_manifest(batch, payload)
    payload[MANIFEST_FILE] = json.dumps(
        manifest, ensure_ascii=False, indent=2
    ).encode("utf-8")

    tmp_p: Optional[Path] = None
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(
            suffix=".zip.tmp",
            prefix=f"audit_{batch_id}_",
            dir=str(output.parent),
        )
        os.close(tmp_fd)
        tmp_p = Path(tmp_path)

        with zipfile.ZipFile(tmp_p, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in sorted(payload.items()):
                zf.writestr(name, data)

        if output.exists() and force:
            try:
                output.unlink()
            except OSError as e:
                return output, AuditPackError(
                    f"无法覆盖已有文件 {output}: {e}",
                    ExitCode.AUDIT_OUTPUT_PERMISSION,
                    {"output_path": str(output), "error": str(e)},
                )

        os.replace(tmp_p, output)
    except Exception as e:
        if tmp_p and tmp_p.exists():
            try:
                tmp_p.unlink()
            except OSError:
                pass
        return output, AuditPackError(
            f"写入审计包失败: {e}",
            ExitCode.IO_ERROR,
            {"output_path": str(output), "error": str(e)},
        )

    return output, None


class AuditVerifyResult:
    def __init__(self):
        self.ok: bool = True
        self.errors: list[str] = []
        self.manifest: Optional[dict] = None
        self.archive_path: Optional[Path] = None

    def add_error(self, msg: str):
        self.ok = False
        self.errors.append(msg)


def verify_audit_pack(archive_path: str | Path) -> AuditVerifyResult:
    result = AuditVerifyResult()
    archive = Path(archive_path)
    result.archive_path = archive

    if not archive.exists():
        result.add_error(f"归档文件不存在: {archive}")
        return result

    if not zipfile.is_zipfile(archive):
        result.add_error(f"不是有效的 zip 文件: {archive}")
        return result

    try:
        with zipfile.ZipFile(archive, "r") as zf:
            names = set(zf.namelist())

            if MANIFEST_FILE not in names:
                result.add_error(f"归档缺少 {MANIFEST_FILE}")
                return result

            try:
                manifest_raw = zf.read(MANIFEST_FILE)
                manifest = json.loads(manifest_raw.decode("utf-8"))
                result.manifest = manifest
            except Exception as e:
                result.add_error(f"解析 manifest 失败: {e}")
                return result

            manifest_files = manifest.get("files_sha256", {})
            for fname, expected_sha in manifest_files.items():
                if fname not in names:
                    result.add_error(f"manifest 声明的文件在归档中缺失: {fname}")
                    continue
                try:
                    data = zf.read(fname)
                    actual_sha = _sha256_bytes(data)
                    if actual_sha != expected_sha:
                        result.add_error(
                            f"文件 SHA256 不匹配: {fname} (期望 {expected_sha[:12]}..., 实际 {actual_sha[:12]}...)"
                        )
                except Exception as e:
                    result.add_error(f"读取 {fname} 失败: {e}")

            if PRECHECK_REPORT_FILE in names:
                try:
                    precheck = json.loads(zf.read(PRECHECK_REPORT_FILE).decode("utf-8"))
                    actual_batch_id = precheck.get("batch_id")
                    manifest_batch_id = manifest.get("batch_id")
                    if actual_batch_id and manifest_batch_id and actual_batch_id != manifest_batch_id:
                        result.add_error(
                            f"批次号不一致: manifest={manifest_batch_id}, precheck_report={actual_batch_id}"
                        )

                    expected_count = manifest.get("counts", {}).get("precheck_items", -1)
                    actual_count = len(precheck.get("items", []))
                    if expected_count >= 0 and actual_count != expected_count:
                        result.add_error(
                            f"预检明细数量不一致: manifest 声明 {expected_count}, 实际 {actual_count}"
                        )
                except Exception as e:
                    result.add_error(f"校验预检报告失败: {e}")

            if CONFIG_SNAPSHOT_FILE in names:
                try:
                    snap = json.loads(zf.read(CONFIG_SNAPSHOT_FILE).decode("utf-8"))
                    cfg = snap.get("config", {})
                    actual_digest = _dict_digest(cfg)
                    expected_digest = manifest.get("config_digest_sha256", "")
                    if expected_digest and actual_digest != expected_digest:
                        result.add_error(
                            f"配置摘要不一致: manifest 声明 {expected_digest[:12]}..., 实际 {actual_digest[:12]}..."
                        )
                except Exception as e:
                    result.add_error(f"校验配置摘要失败: {e}")

            if DISPATCH_REPORT_FILE in names:
                try:
                    dispatch = json.loads(zf.read(DISPATCH_REPORT_FILE).decode("utf-8"))
                    expected_count = manifest.get("counts", {}).get("dispatch_items", -1)
                    actual_count = len(dispatch.get("items", []))
                    if expected_count >= 0 and actual_count != expected_count:
                        result.add_error(
                            f"发放明细数量不一致: manifest 声明 {expected_count}, 实际 {actual_count}"
                        )
                except Exception as e:
                    result.add_error(f"校验发放明细失败: {e}")

            if ROLLBACK_REPORT_FILE in names:
                try:
                    rollback = json.loads(zf.read(ROLLBACK_REPORT_FILE).decode("utf-8"))
                    expected_count = manifest.get("counts", {}).get("rollback_results", -1)
                    actual_count = len(rollback.get("results", []))
                    if expected_count >= 0 and actual_count != expected_count:
                        result.add_error(
                            f"回滚记录数量不一致: manifest 声明 {expected_count}, 实际 {actual_count}"
                        )
                except Exception as e:
                    result.add_error(f"校验回滚记录失败: {e}")

    except zipfile.BadZipFile:
        result.add_error(f"zip 损坏: {archive}")
    except Exception as e:
        result.add_error(f"读取归档失败: {e}")

    return result
