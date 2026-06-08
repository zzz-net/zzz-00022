from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import (
    BatchStatus,
    DispatchConfig,
    DispatchItem,
    PreCheckReport,
    PreviewReport,
    RoomRow,
    SignoffReport,
    SignoffAuditEntry,
    SignoffAuditAction,
    SIGNOFF_AUDIT_LOG_FILE,
    SIGNOFF_ROOM_VERSIONS_FILE,
    gen_signoff_audit_id,
    IncidentTicket,
    IncidentStatus,
    IncidentAuditEntry,
    IncidentAuditAction,
    INCIDENT_AUDIT_LOG_FILE,
    INCIDENTS_DIR,
    INCIDENT_INDEX_FILE,
    gen_incident_id,
    gen_incident_handling_id,
    gen_incident_audit_id,
)


BATCH_INDEX_FILE = "batches.json"
CONFIG_SNAPSHOT_FILE = "config_snapshot.json"
PRECHECK_REPORT_FILE = "precheck_report.json"
DISPATCH_REPORT_FILE = "dispatch_report.json"
ROLLBACK_REPORT_FILE = "rollback_report.json"
EVENTS_LOG_FILE = "events.log"
PREVIEWS_DIR = "previews"
PREVIEW_INDEX_FILE = "previews_index.json"
SIGNOFFS_DIR = "signoffs"
SIGNOFF_INDEX_FILE = "signoffs_index.json"


def gen_batch_id() -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"batch-{ts}-{uuid.uuid4().hex[:6]}"


def gen_preview_id() -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"preview-{ts}-{uuid.uuid4().hex[:6]}"


def gen_signoff_id() -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"signoff-{ts}-{uuid.uuid4().hex[:6]}"


class BatchState:
    def __init__(self, batch_id: str, storage_dir: str | Path):
        self.batch_id = batch_id
        self.storage_dir = Path(storage_dir)
        self.batch_dir = self.storage_dir / batch_id
        self.status: BatchStatus = BatchStatus.PENDING
        self.created_at: str = datetime.now().isoformat()
        self.updated_at: str = self.created_at
        self.config_snapshot: Optional[dict] = None
        self.csv_path: Optional[str] = None
        self.total_items: int = 0
        self.success_count: int = 0
        self.fail_count: int = 0
        self.notes: str = ""

    @classmethod
    def create(cls, storage_dir: str | Path, batch_id: Optional[str] = None) -> "BatchState":
        batch_id = batch_id or gen_batch_id()
        state = cls(batch_id, storage_dir)
        state.batch_dir.mkdir(parents=True, exist_ok=True)
        return state

    def set_status(self, status: BatchStatus, notes: str = ""):
        self.status = status
        self.updated_at = datetime.now().isoformat()
        if notes:
            self.notes = notes
        self._log_event(f"状态变更 -> {status.value}" + (f": {notes}" if notes else ""))
        self.save()

    def save_config_snapshot(self, config: DispatchConfig, csv_path: str | Path):
        snapshot = {
            "config": config.model_dump(),
            "csv_path": str(csv_path),
            "saved_at": datetime.now().isoformat(),
        }
        (self.batch_dir / CONFIG_SNAPSHOT_FILE).write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.config_snapshot = snapshot
        self.csv_path = str(csv_path)
        self._log_event(f"保存配置快照 (csv={csv_path})")
        self.save()

    def load_config_snapshot(self) -> Optional[dict]:
        p = self.batch_dir / CONFIG_SNAPSHOT_FILE
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            self.config_snapshot = data
            self.csv_path = data.get("csv_path")
            return data
        return self.config_snapshot

    def save_precheck_report(self, report: PreCheckReport):
        (self.batch_dir / PRECHECK_REPORT_FILE).write_text(
            json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.total_items = len(report.items)
        self._log_event(f"保存预检报告: {'通过' if report.passed else '未通过'}, {len(report.items)} 项")
        self.save()

    def load_precheck_report(self) -> Optional[PreCheckReport]:
        p = self.batch_dir / PRECHECK_REPORT_FILE
        if not p.exists():
            return None
        return PreCheckReport.model_validate_json(p.read_text(encoding="utf-8"))

    def save_dispatch_report(self, items: list[DispatchItem]):
        data = {
            "batch_id": self.batch_id,
            "saved_at": datetime.now().isoformat(),
            "items": [it.model_dump() for it in items],
        }
        (self.batch_dir / DISPATCH_REPORT_FILE).write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.success_count = sum(1 for it in items if it.dispatched)
        self.fail_count = sum(1 for it in items if not it.dispatched)
        self._log_event(f"保存发放报告: 成功 {self.success_count}, 失败 {self.fail_count}")
        self.save()

    def load_dispatch_report(self) -> Optional[dict]:
        p = self.batch_dir / DISPATCH_REPORT_FILE
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def save_rollback_report(self, results: list[dict]):
        data = {
            "batch_id": self.batch_id,
            "saved_at": datetime.now().isoformat(),
            "results": results,
        }
        (self.batch_dir / ROLLBACK_REPORT_FILE).write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._log_event(f"保存回滚报告: {len(results)} 项")
        self.save()

    def load_rollback_report(self) -> Optional[dict]:
        p = self.batch_dir / ROLLBACK_REPORT_FILE
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def save_preview_report(self, report: PreviewReport) -> Path:
        previews_dir = self.batch_dir / PREVIEWS_DIR
        previews_dir.mkdir(parents=True, exist_ok=True)
        target = previews_dir / f"{report.preview_id}.json"
        target.write_text(
            json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._update_preview_index(report)
        self._log_event(f"保存预演报告: {report.preview_id}, passed={report.passed}")
        self.save()
        return target

    def list_preview_ids(self) -> list[str]:
        previews_dir = self.batch_dir / PREVIEWS_DIR
        if not previews_dir.exists():
            return []
        files = sorted(
            previews_dir.glob("preview-*.json"),
            key=lambda p: (p.stat().st_mtime, p.name),
        )
        return [p.stem for p in files]

    def load_preview_report(self, preview_id: str) -> Optional[PreviewReport]:
        p = self.batch_dir / PREVIEWS_DIR / f"{preview_id}.json"
        if not p.exists():
            return None
        return PreviewReport.model_validate_json(p.read_text(encoding="utf-8"))

    def load_all_preview_reports(self) -> list[PreviewReport]:
        reports = []
        for pid in self.list_preview_ids():
            rpt = self.load_preview_report(pid)
            if rpt:
                reports.append(rpt)
        return reports

    def _update_preview_index(self, report: PreviewReport):
        idx_path = self.batch_dir / PREVIEWS_DIR / PREVIEW_INDEX_FILE
        if idx_path.exists():
            data = json.loads(idx_path.read_text(encoding="utf-8"))
        else:
            data = {}
        data[report.preview_id] = {
            "preview_id": report.preview_id,
            "previewed_at": report.previewed_at,
            "passed": report.passed,
            "total_rows": report.total_rows,
            "valid_rows": report.valid_rows,
            "csv_path": report.csv_path,
        }
        idx_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load_preview_index(self) -> dict:
        idx_path = self.batch_dir / PREVIEWS_DIR / PREVIEW_INDEX_FILE
        if not idx_path.exists():
            return {}
        return json.loads(idx_path.read_text(encoding="utf-8"))

    def save_signoff_report(self, report: SignoffReport) -> Path:
        signoffs_dir = self.batch_dir / SIGNOFFS_DIR
        signoffs_dir.mkdir(parents=True, exist_ok=True)
        target = signoffs_dir / f"{report.signoff_id}.json"
        target.write_text(
            json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._update_signoff_index(report)
        self._log_event(
            f"保存签收报告: {report.signoff_id}, "
            f"passed={report.passed}, "
            f"signed={report.signed_rooms}, "
            f"abnormal={report.abnormal_count}"
        )
        self.save()
        return target

    def list_signoff_ids(self) -> list[str]:
        signoffs_dir = self.batch_dir / SIGNOFFS_DIR
        if not signoffs_dir.exists():
            return []
        files = sorted(
            signoffs_dir.glob("signoff-*.json"),
            key=lambda p: (p.stat().st_mtime, p.name),
        )
        return [p.stem for p in files]

    def load_signoff_report(self, signoff_id: str) -> Optional[SignoffReport]:
        p = self.batch_dir / SIGNOFFS_DIR / f"{signoff_id}.json"
        if not p.exists():
            return None
        return SignoffReport.model_validate_json(p.read_text(encoding="utf-8"))

    def load_all_signoff_reports(self) -> list[SignoffReport]:
        reports = []
        for sid in self.list_signoff_ids():
            rpt = self.load_signoff_report(sid)
            if rpt:
                reports.append(rpt)
        return reports

    def load_latest_signoff_report(self) -> Optional[SignoffReport]:
        ids = self.list_signoff_ids()
        if not ids:
            return None
        return self.load_signoff_report(ids[-1])

    def _update_signoff_index(self, report: SignoffReport):
        idx_path = self.batch_dir / SIGNOFFS_DIR / SIGNOFF_INDEX_FILE
        if idx_path.exists():
            data = json.loads(idx_path.read_text(encoding="utf-8"))
        else:
            data = {}
        data[report.signoff_id] = {
            "signoff_id": report.signoff_id,
            "imported_at": report.imported_at,
            "passed": report.passed,
            "total_rows": report.total_rows,
            "valid_rows": report.valid_rows,
            "signed_rooms": report.signed_rooms,
            "abnormal_count": report.abnormal_count,
            "csv_path": report.csv_path,
        }
        idx_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load_signoff_index(self) -> dict:
        idx_path = self.batch_dir / SIGNOFFS_DIR / SIGNOFF_INDEX_FILE
        if not idx_path.exists():
            return {}
        return json.loads(idx_path.read_text(encoding="utf-8"))

    def _get_signoff_audit_log_path(self) -> Path:
        return self.batch_dir / SIGNOFFS_DIR / SIGNOFF_AUDIT_LOG_FILE

    def _get_room_versions_path(self) -> Path:
        return self.batch_dir / SIGNOFFS_DIR / SIGNOFF_ROOM_VERSIONS_FILE

    def append_signoff_audit(self, entry: SignoffAuditEntry) -> None:
        signoffs_dir = self.batch_dir / SIGNOFFS_DIR
        signoffs_dir.mkdir(parents=True, exist_ok=True)
        audit_path = self._get_signoff_audit_log_path()
        line = json.dumps(entry.model_dump(mode="json"), ensure_ascii=False) + "\n"
        with audit_path.open("a", encoding="utf-8") as f:
            f.write(line)
        self._log_event(
            f"签收审计日志: action={entry.action.value}, "
            f"room={entry.room_id}/{entry.subject}, "
            f"operator={entry.operator}, "
            f"version {entry.version_before}->{entry.version_after}"
        )

    def load_signoff_audit_log(self) -> list[SignoffAuditEntry]:
        audit_path = self._get_signoff_audit_log_path()
        if not audit_path.exists():
            return []
        entries: list[SignoffAuditEntry] = []
        with audit_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(SignoffAuditEntry.model_validate_json(line))
        return entries

    def load_room_versions(self) -> dict:
        vpath = self._get_room_versions_path()
        if not vpath.exists():
            return {}
        return json.loads(vpath.read_text(encoding="utf-8"))

    def save_room_versions(self, versions: dict) -> None:
        signoffs_dir = self.batch_dir / SIGNOFFS_DIR
        signoffs_dir.mkdir(parents=True, exist_ok=True)
        vpath = self._get_room_versions_path()
        vpath.write_text(
            json.dumps(versions, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get_room_version(self, exam_id: str, room_id: str, subject: str) -> int:
        versions = self.load_room_versions()
        key = f"{exam_id}:{room_id}:{subject}"
        return versions.get(key, 0)

    def increment_room_version(self, exam_id: str, room_id: str, subject: str) -> int:
        versions = self.load_room_versions()
        key = f"{exam_id}:{room_id}:{subject}"
        versions[key] = versions.get(key, 0) + 1
        self.save_room_versions(versions)
        return versions[key]

    def get_effective_signoff_items(self) -> dict[tuple[str, str, str], dict]:
        """获取当前有效的签收项（考虑更正和撤销）。"""
        latest = self.load_latest_signoff_report()
        if not latest:
            return {}
        result: dict[tuple[str, str, str], dict] = {}
        for it in latest.signoff_items:
            key = (it["exam_id"], it["room_id"], it["subject"])
            result[key] = dict(it)
        audit_entries = self.load_signoff_audit_log()
        for entry in audit_entries:
            key = (entry.exam_id, entry.room_id, entry.subject)
            if entry.action == SignoffAuditAction.CORRECT:
                if key in result:
                    current = result[key]
                    for field, new_val in entry.new_values.items():
                        current[field] = new_val
                    current["version"] = entry.version_after
                    current["last_updated"] = entry.timestamp
                    current["last_operator"] = entry.operator
                    precheck = self.load_precheck_report()
                    expected = None
                    if precheck:
                        for item in precheck.items:
                            rr = item.room_row
                            if (rr.exam_id, rr.room_id, rr.subject) == key:
                                expected = rr.students_count
                                break
                    current["is_abnormal"] = bool(current.get("damage_note")) or (
                        expected is not None and current.get("received_count") != expected
                    )
            elif entry.action == SignoffAuditAction.REVOKE:
                if key in result:
                    result[key]["revoked"] = True
                    result[key]["revoked_at"] = entry.timestamp
                    result[key]["revoked_by"] = entry.operator
                    result[key]["revoke_reason"] = entry.reason
            elif entry.action == SignoffAuditAction.IMPORT:
                if key in result:
                    for field in ("revoked", "revoked_at", "revoked_by", "revoke_reason"):
                        result[key].pop(field, None)
                    result[key]["version"] = entry.version_after
                    result[key]["last_updated"] = entry.timestamp
                    result[key]["last_operator"] = entry.operator
        return result

    def get_signoff_room_history(
        self, exam_id: str, room_id: str, subject: str
    ) -> dict:
        """获取某个考场的签收历史（含所有版本和审计记录）。"""
        key = (exam_id, room_id, subject)
        history: dict = {
            "exam_id": exam_id,
            "room_id": room_id,
            "subject": subject,
            "current_version": self.get_room_version(exam_id, room_id, subject),
            "imported": False,
            "revoked": False,
            "import_records": [],
            "audit_records": [],
            "current": None,
        }
        all_reports = self.load_all_signoff_reports()
        for rpt in all_reports:
            for it in rpt.signoff_items:
                if (it["exam_id"], it["room_id"], it["subject"]) == key:
                    history["import_records"].append({
                        "signoff_id": rpt.signoff_id,
                        "imported_at": rpt.imported_at,
                        "values": dict(it),
                    })
                    history["imported"] = True
        effective = self.get_effective_signoff_items()
        if key in effective:
            history["current"] = effective[key]
            if effective[key].get("revoked"):
                history["revoked"] = True
        all_audit = self.load_signoff_audit_log()
        for entry in all_audit:
            if (entry.exam_id, entry.room_id, entry.subject) == key:
                history["audit_records"].append(entry.model_dump())
        history["audit_records"].sort(key=lambda x: x["timestamp"])
        return history

    def _get_incidents_dir(self) -> Path:
        return self.batch_dir / INCIDENTS_DIR

    def _get_incident_audit_log_path(self) -> Path:
        return self._get_incidents_dir() / INCIDENT_AUDIT_LOG_FILE

    def append_incident_audit(self, entry: IncidentAuditEntry) -> None:
        incidents_dir = self._get_incidents_dir()
        incidents_dir.mkdir(parents=True, exist_ok=True)
        audit_path = self._get_incident_audit_log_path()
        line = json.dumps(entry.model_dump(mode="json"), ensure_ascii=False) + "\n"
        with audit_path.open("a", encoding="utf-8") as f:
            f.write(line)
        self._log_event(
            f"异常处置单审计: action={entry.action.value}, "
            f"ticket={entry.ticket_id}, "
            f"room={entry.room_id}/{entry.subject}, "
            f"operator={entry.operator}"
        )

    def load_incident_audit_log(self) -> list[IncidentAuditEntry]:
        audit_path = self._get_incident_audit_log_path()
        if not audit_path.exists():
            return []
        entries: list[IncidentAuditEntry] = []
        with audit_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(IncidentAuditEntry.model_validate_json(line))
        return entries

    def save_incident(self, ticket: IncidentTicket) -> Path:
        incidents_dir = self._get_incidents_dir()
        incidents_dir.mkdir(parents=True, exist_ok=True)
        target = incidents_dir / f"{ticket.ticket_id}.json"
        target.write_text(
            json.dumps(ticket.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._update_incident_index(ticket)
        self._log_event(
            f"异常处置单: {ticket.ticket_id}, "
            f"type={ticket.incident_type.value}, "
            f"room={ticket.room_id}/{ticket.subject}, "
            f"status={ticket.status.value}"
        )
        self.save()
        return target

    def list_incident_ids(self) -> list[str]:
        incidents_dir = self._get_incidents_dir()
        if not incidents_dir.exists():
            return []
        files = sorted(
            incidents_dir.glob("incident-*.json"),
            key=lambda p: (p.stat().st_mtime, p.name),
        )
        return [p.stem for p in files]

    def load_incident(self, ticket_id: str) -> Optional[IncidentTicket]:
        p = self._get_incidents_dir() / f"{ticket_id}.json"
        if not p.exists():
            return None
        return IncidentTicket.model_validate_json(p.read_text(encoding="utf-8"))

    def load_all_incidents(self) -> list[IncidentTicket]:
        tickets = []
        for tid in self.list_incident_ids():
            t = self.load_incident(tid)
            if t:
                tickets.append(t)
        return tickets

    def load_open_incidents(self) -> list[IncidentTicket]:
        return [t for t in self.load_all_incidents() if t.status != IncidentStatus.CLOSED]

    def find_open_incident_by_room(self, exam_id: str, room_id: str, subject: str) -> Optional[IncidentTicket]:
        for t in self.load_open_incidents():
            if t.exam_id == exam_id and t.room_id == room_id and t.subject == subject:
                return t
        return None

    def _update_incident_index(self, ticket: IncidentTicket):
        idx_path = self._get_incidents_dir() / INCIDENT_INDEX_FILE
        if idx_path.exists():
            data = json.loads(idx_path.read_text(encoding="utf-8"))
        else:
            data = {}
        data[ticket.ticket_id] = {
            "ticket_id": ticket.ticket_id,
            "created_at": ticket.created_at,
            "status": ticket.status.value,
            "exam_id": ticket.exam_id,
            "room_id": ticket.room_id,
            "subject": ticket.subject,
            "incident_type": ticket.incident_type.value,
            "operator": ticket.operator,
            "closed_at": ticket.closed_at,
            "handling_count": len(ticket.handling_records),
        }
        idx_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load_incident_index(self) -> dict:
        idx_path = self._get_incidents_dir() / INCIDENT_INDEX_FILE
        if not idx_path.exists():
            return {}
        return json.loads(idx_path.read_text(encoding="utf-8"))

    def count_incidents(self) -> dict:
        all_tickets = self.load_all_incidents()
        return {
            "total": len(all_tickets),
            "open": sum(1 for t in all_tickets if t.status == IncidentStatus.OPEN),
            "processing": sum(1 for t in all_tickets if t.status == IncidentStatus.PROCESSING),
            "closed": sum(1 for t in all_tickets if t.status == IncidentStatus.CLOSED),
        }

    def _log_event(self, message: str):
        ts = datetime.now().isoformat()
        line = f"[{ts}] [{self.batch_id}] {message}\n"
        (self.storage_dir / EVENTS_LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
        with (self.storage_dir / EVENTS_LOG_FILE).open("a", encoding="utf-8") as f:
            f.write(line)

    def to_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "csv_path": self.csv_path,
            "total_items": self.total_items,
            "success_count": self.success_count,
            "fail_count": self.fail_count,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict, storage_dir: str | Path) -> "BatchState":
        state = cls(data["batch_id"], storage_dir)
        state.status = BatchStatus(data["status"])
        state.created_at = data.get("created_at", state.created_at)
        state.updated_at = data.get("updated_at", state.updated_at)
        state.csv_path = data.get("csv_path")
        state.total_items = data.get("total_items", 0)
        state.success_count = data.get("success_count", 0)
        state.fail_count = data.get("fail_count", 0)
        state.notes = data.get("notes", "")
        return state

    def save(self):
        index_path = self.storage_dir / BATCH_INDEX_FILE
        if index_path.exists():
            data = json.loads(index_path.read_text(encoding="utf-8"))
        else:
            data = {}
        data[self.batch_id] = self.to_dict()
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


class Storage:
    def __init__(self, storage_dir: str | Path):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self.storage_dir / BATCH_INDEX_FILE
        self._batches: dict[str, BatchState] = {}
        self._load_index()

    def _load_index(self):
        if self._index_path.exists():
            data = json.loads(self._index_path.read_text(encoding="utf-8"))
            for bid, bdata in data.items():
                self._batches[bid] = BatchState.from_dict(bdata, self.storage_dir)

    def _write_index(self):
        data = {bid: bs.to_dict() for bid, bs in self._batches.items()}
        self._index_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def create_batch(self, batch_id: Optional[str] = None) -> BatchState:
        if batch_id is not None and (batch_id in self._batches or (self.storage_dir / batch_id).exists()):
            raise ValueError(
                f"批次 ID 已存在，禁止复用覆盖: {batch_id}。"
                "如需继续请更换 batch-id，或先清理 .exam_dispatch_state/ 中对应批次目录。"
            )
        bs = BatchState.create(self.storage_dir, batch_id)
        self._batches[bs.batch_id] = bs
        self._write_index()
        return bs

    def get_batch(self, batch_id: str) -> Optional[BatchState]:
        return self._batches.get(batch_id)

    def list_batches(self) -> list[BatchState]:
        return sorted(
            self._batches.values(),
            key=lambda b: b.created_at,
            reverse=True,
        )

    def update_batch(self, batch: BatchState):
        self._batches[batch.batch_id] = batch
        self._write_index()

    def get_events_log(self) -> str:
        p = self.storage_dir / EVENTS_LOG_FILE
        if not p.exists():
            return ""
        return p.read_text(encoding="utf-8")

    def get_events_for_batch(self, batch_id: str) -> str:
        full_log = self.get_events_log()
        if not full_log:
            return ""
        marker = f"[{batch_id}]"
        lines = [l for l in full_log.splitlines() if marker in l]
        return "\n".join(lines) + ("\n" if lines else "")

    def list_all_previews(self) -> list[dict]:
        results = []
        for batch in self.list_batches():
            idx = batch.load_preview_index()
            for pid, meta in idx.items():
                entry = {"batch_id": batch.batch_id}
                entry.update(meta)
                results.append(entry)
        results.sort(key=lambda x: x.get("previewed_at", ""), reverse=True)
        return results

    def list_all_signoffs(self) -> list[dict]:
        results = []
        for batch in self.list_batches():
            idx = batch.load_signoff_index()
            for sid, meta in idx.items():
                entry = {"batch_id": batch.batch_id}
                entry.update(meta)
                results.append(entry)
        results.sort(key=lambda x: x.get("imported_at", ""), reverse=True)
        return results

    def list_all_incidents(self) -> list[dict]:
        results = []
        for batch in self.list_batches():
            idx = batch.load_incident_index()
            for tid, meta in idx.items():
                entry = {"batch_id": batch.batch_id}
                entry.update(meta)
                results.append(entry)
        results.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return results
