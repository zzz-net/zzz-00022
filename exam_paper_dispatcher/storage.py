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
    RoomRow,
)


BATCH_INDEX_FILE = "batches.json"
CONFIG_SNAPSHOT_FILE = "config_snapshot.json"
PRECHECK_REPORT_FILE = "precheck_report.json"
DISPATCH_REPORT_FILE = "dispatch_report.json"
ROLLBACK_REPORT_FILE = "rollback_report.json"
EVENTS_LOG_FILE = "events.log"


def gen_batch_id() -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"batch-{ts}-{uuid.uuid4().hex[:6]}"


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
