from __future__ import annotations

import csv
import json
import os
import zipfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from exam_paper_dispatcher.cli import main
from exam_paper_dispatcher.models import (
    BatchStatus,
    DispatchConfig,
    ExitCode,
    RoomRow,
    compute_sha256,
)
from exam_paper_dispatcher.storage import Storage


REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"


@pytest.fixture()
def runner():
    return CliRunner()


@pytest.fixture()
def fresh_env(tmp_path, monkeypatch):
    """隔离存储目录和输出目录。"""
    storage_dir = tmp_path / "state"
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    papers_src = EXAMPLES / "papers"
    (tmp_path / "papers").mkdir()
    for p in papers_src.iterdir():
        (tmp_path / "papers" / p.name).write_bytes(p.read_bytes())

    config = {
        "source_root": str(tmp_path / "papers"),
        "output_root": str(output_dir),
        "default_subjects": ["math", "chinese", "english"],
        "subject_versions": {
            "math": "2026-spring-v1",
            "chinese": "2026-spring-v1",
            "english": "2026-spring-v2",
        },
        "package_format": "zip",
        "naming_pattern": "{exam_id}_{room_id}_{subject}",
        "storage_dir": str(storage_dir),
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    rooms_path = tmp_path / "rooms.csv"
    rooms_path.write_text(
        "exam_id,room_id,subject,students_count,source_file,target_name\n"
        "20260608,A101,math,30,paper_math_v1.pdf,20260608_A101_math\n"
        "20260608,A102,math,28,paper_math_v1.pdf,20260608_A102_math\n"
        "20260608,B201,english,35,paper_english_v2.pdf,20260608_B201_english\n",
        encoding="utf-8",
    )

    return {
        "tmp_path": tmp_path,
        "storage_dir": storage_dir,
        "output_dir": output_dir,
        "config_path": config_path,
        "rooms_path": rooms_path,
    }


# ---------------------------------------------------------------------------
# 1. 成功预检 / 发放 / 查询 / 导出
# ---------------------------------------------------------------------------

def test_successful_precheck_dispatch_query_export(runner, fresh_env):
    env = fresh_env

    # --- precheck ---
    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "precheck",
            "--config", str(env["config_path"]),
            "--rooms", str(env["rooms_path"]),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert "预检通过" in r.stdout
    assert "总行数: 3" in r.stdout

    storage = Storage(env["storage_dir"])
    batches = storage.list_batches()
    assert len(batches) == 1
    batch = batches[0]
    assert batch.status == BatchStatus.DRY_RUN_PASSED
    bid = batch.batch_id

    # --- dispatch ---
    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "dispatch",
            "--batch-id", bid,
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert "成功 3/3" in r.stdout

    # 验证 zip 产物存在且可解压
    for target in ("20260608_A101_math.zip", "20260608_A102_math.zip", "20260608_B201_english.zip"):
        zpath = env["output_dir"] / target
        assert zpath.exists(), f"缺少产物 {target}"
        with zipfile.ZipFile(zpath) as zf:
            assert zf.namelist()  # 非空

    batch = Storage(env["storage_dir"]).get_batch(bid)
    assert batch.status == BatchStatus.COMPLETED
    assert batch.success_count == 3
    assert batch.fail_count == 0

    # --- query list ---
    r = runner.invoke(
        main,
        ["--storage-dir", str(env["storage_dir"]), "query"],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS
    assert "批次列表" in r.stdout
    assert "3/0" in r.stdout  # success/fail 摘要
    listed = Storage(env["storage_dir"]).list_batches()
    assert len(listed) == 1
    assert listed[0].status == BatchStatus.COMPLETED

    # --- query detail ---
    r = runner.invoke(
        main,
        ["--storage-dir", str(env["storage_dir"]), "query", "--batch-id", bid],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS
    detail = json.loads(r.stdout)
    assert detail["batch_id"] == bid
    assert detail["dispatch"] is not None
    assert len(detail["dispatch"]["items_summary"]) == 3

    # --- export JSON (no stray "导出失败") ---
    out_json = env["tmp_path"] / "all.json"
    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "export",
            "--format", "json",
            "--output", str(out_json),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert "导出失败" not in r.stdout
    assert "导出失败" not in (r.stderr or "")
    assert out_json.exists()
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert "batches" in data
    assert len(data["batches"]) == 1
    assert "events_log" in data

    # --- export CSV batches ---
    out_csv = env["tmp_path"] / "batches.csv"
    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "export", "--format", "csv",
            "--output", str(out_csv),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS
    assert "导出失败" not in (r.stdout + (r.stderr or ""))
    with out_csv.open("r", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["batch_id"] == bid
    assert rows[0]["status"] == "completed"

    # --- export CSV items ---
    out_items = env["tmp_path"] / "items.csv"
    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "export", "--format", "csv-items",
            "--batch-id", bid,
            "--output", str(out_items),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS
    assert "导出失败" not in (r.stdout + (r.stderr or ""))
    with out_items.open("r", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 3
    for row in rows:
        assert row["dispatched"] in ("True", "true", True)
        assert row["target_sha256"]  # 发放后必须有目标 SHA


# ---------------------------------------------------------------------------
# 2. 重复 batch-id：明确拒绝，状态、日志不能被改坏
# ---------------------------------------------------------------------------

def test_batch_id_conflict_rejected(runner, fresh_env):
    env = fresh_env

    # 第一次预检成功
    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "precheck", "--config", str(env["config_path"]),
            "--rooms", str(env["rooms_path"]),
            "--batch-id", "fixed-batch-001",
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS

    storage = Storage(env["storage_dir"])
    batch_before = storage.get_batch("fixed-batch-001")
    assert batch_before.status == BatchStatus.DRY_RUN_PASSED
    first_created = batch_before.created_at

    # 用另一份清单 + 相同 batch-id → 必须被拒绝
    other_rooms = env["tmp_path"] / "other.csv"
    other_rooms.write_text(
        "exam_id,room_id,subject,students_count,source_file,target_name\n"
        "99999999,Z999,math,1,paper_math_v1.pdf,99999999_Z999_math\n",
        encoding="utf-8",
    )
    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "precheck", "--config", str(env["config_path"]),
            "--rooms", str(other_rooms),
            "--batch-id", "fixed-batch-001",
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.BATCH_ID_CONFLICT
    assert "已存在" in (r.stdout + (r.stderr or ""))

    # 原始批次状态、创建时间、CSV 路径均未被改坏
    batch_after = Storage(env["storage_dir"]).get_batch("fixed-batch-001")
    assert batch_after.status == BatchStatus.DRY_RUN_PASSED
    assert batch_after.created_at == first_created
    assert "other" not in (batch_after.csv_path or "")


# ---------------------------------------------------------------------------
# 3. 失败退出码：缺失源文件、目标名冲突
# ---------------------------------------------------------------------------

def test_exit_code_missing_source(runner, fresh_env):
    env = fresh_env
    bad_rooms = env["tmp_path"] / "bad.csv"
    bad_rooms.write_text(
        "exam_id,room_id,subject,students_count,source_file,target_name\n"
        "20260608,A101,math,30,DOES_NOT_EXIST.pdf,missing_target\n",
        encoding="utf-8",
    )
    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "precheck", "--config", str(env["config_path"]),
            "--rooms", str(bad_rooms),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.MISSING_SOURCE, f"got {r.exit_code}, stdout={r.stdout}"
    # 缺失源文件时不能落已完成
    batches = Storage(env["storage_dir"]).list_batches()
    assert len(batches) == 1
    assert batches[0].status == BatchStatus.FAILED


def test_exit_code_target_conflict(runner, fresh_env):
    env = fresh_env
    conflict_csv = env["tmp_path"] / "conflict.csv"
    conflict_csv.write_text(
        "exam_id,room_id,subject,students_count,source_file,target_name\n"
        "20260608,A101,math,30,paper_math_v1.pdf,SAME_NAME\n"
        "20260608,A102,math,28,paper_math_v1.pdf,SAME_NAME\n",
        encoding="utf-8",
    )
    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "precheck", "--config", str(env["config_path"]),
            "--rooms", str(conflict_csv),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.TARGET_CONFLICT, f"got {r.exit_code}, stdout={r.stdout}"
    assert "SAME_NAME" in r.stdout


# ---------------------------------------------------------------------------
# 4. 回滚篡改检测：SHA 不匹配则停止并返回退出码 9
# ---------------------------------------------------------------------------

def test_rollback_tamper_detection(runner, fresh_env):
    env = fresh_env

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "precheck", "--config", str(env["config_path"]),
            "--rooms", str(env["rooms_path"]),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS
    bid = Storage(env["storage_dir"]).list_batches()[0].batch_id

    r = runner.invoke(
        main,
        ["--storage-dir", str(env["storage_dir"]), "dispatch", "--batch-id", bid],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS

    # 篡改其中一个产物
    target = env["output_dir"] / "20260608_A101_math.zip"
    original_sha = compute_sha256(target)
    target.write_bytes(b"TAMPERED_CONTENT_BY_EVIL")
    assert compute_sha256(target) != original_sha

    # 回滚应被拦截
    r = runner.invoke(
        main,
        ["--storage-dir", str(env["storage_dir"]), "rollback", "--batch-id", bid],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.ROLLBACK_TAMPERED, f"got {r.exit_code}, stdout={r.stdout}"
    assert "被第三方修改" in (r.stdout + (r.stderr or ""))
    # 剩余未被处理的产物仍保留（回滚在篡改处停止）
    assert (env["output_dir"] / "20260608_A102_math.zip").exists()

    # 强制回滚
    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "rollback", "--batch-id", bid, "--force",
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS, f"stdout={r.stdout}"
    # 所有产物均被清理
    assert not any(env["output_dir"].iterdir())

    batch = Storage(env["storage_dir"]).get_batch(bid)
    assert batch.status == BatchStatus.ROLLED_BACK
