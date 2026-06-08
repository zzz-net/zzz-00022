from __future__ import annotations

import csv
import json
import os
import tempfile
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


# ---------------------------------------------------------------------------
# 5. audit-pack / audit-verify: 成功链路（预检后、发放后、回滚后）
# ---------------------------------------------------------------------------

def test_audit_pack_and_verify_after_precheck(runner, fresh_env):
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

    out_zip = env["tmp_path"] / f"audit_{bid}.zip"
    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "audit-pack", "--batch-id", bid,
            "--output", str(out_zip),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert out_zip.exists()
    assert out_zip.stat().st_size > 0

    with zipfile.ZipFile(out_zip, "r") as zf:
        names = set(zf.namelist())
        assert "manifest.json" in names
        assert "README.txt" in names
        assert "config_snapshot.json" in names
        assert "precheck_report.json" in names
        assert "batch_events.log" in names
        assert "dispatch_report.json" not in names
        assert "rollback_report.json" not in names
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert manifest["batch_id"] == bid
        assert manifest["batch_status"] == "dry_run_passed"
        assert manifest["counts"]["precheck_items"] == 3
        assert manifest["counts"]["dispatch_items"] == 0
        assert manifest["config_digest_sha256"]
        readme = zf.read("README.txt").decode("utf-8")
        assert bid in readme
        assert "考务交接审计包" in readme

    r = runner.invoke(
        main,
        ["audit-verify", "--archive", str(out_zip)],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert "审计包校验通过" in r.stdout
    assert bid in r.stdout


def test_audit_pack_and_verify_after_dispatch(runner, fresh_env):
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
    bid = Storage(env["storage_dir"]).list_batches()[0].batch_id
    r = runner.invoke(
        main,
        ["--storage-dir", str(env["storage_dir"]), "dispatch", "--batch-id", bid],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS

    out_zip = env["tmp_path"] / f"audit_{bid}.zip"
    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "audit-pack", "--batch-id", bid,
            "--output", str(out_zip),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS

    with zipfile.ZipFile(out_zip, "r") as zf:
        names = set(zf.namelist())
        assert "dispatch_report.json" in names
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert manifest["batch_status"] == "completed"
        assert manifest["counts"]["dispatch_items"] == 3

    r = runner.invoke(
        main,
        ["audit-verify", "--archive", str(out_zip)],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS


def test_audit_pack_and_verify_after_rollback(runner, fresh_env):
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
    bid = Storage(env["storage_dir"]).list_batches()[0].batch_id
    r = runner.invoke(
        main,
        ["--storage-dir", str(env["storage_dir"]), "dispatch", "--batch-id", bid],
        catch_exceptions=False,
    )
    r = runner.invoke(
        main,
        ["--storage-dir", str(env["storage_dir"]), "rollback", "--batch-id", bid],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS

    out_zip = env["tmp_path"] / f"audit_{bid}.zip"
    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "audit-pack", "--batch-id", bid,
            "--output", str(out_zip),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS

    with zipfile.ZipFile(out_zip, "r") as zf:
        names = set(zf.namelist())
        assert "rollback_report.json" in names
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert manifest["batch_status"] == "rolled_back"
        assert manifest["counts"]["rollback_results"] == 3

    r = runner.invoke(
        main,
        ["audit-verify", "--archive", str(out_zip)],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS


# ---------------------------------------------------------------------------
# 6. audit-pack: 跨重启内容一致
# ---------------------------------------------------------------------------

def test_audit_pack_consistent_after_restart(runner, fresh_env):
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
    bid = Storage(env["storage_dir"]).list_batches()[0].batch_id
    r = runner.invoke(
        main,
        ["--storage-dir", str(env["storage_dir"]), "dispatch", "--batch-id", bid],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS

    zip1 = env["tmp_path"] / "audit_v1.zip"
    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "audit-pack", "--batch-id", bid,
            "--output", str(zip1),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS

    zip2 = env["tmp_path"] / "audit_v2.zip"
    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "audit-pack", "--batch-id", bid,
            "--output", str(zip2),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS

    with zipfile.ZipFile(zip1, "r") as zf1, zipfile.ZipFile(zip2, "r") as zf2:
        names1 = sorted(zf1.namelist())
        names2 = sorted(zf2.namelist())
        assert names1 == names2
        for n in names1:
            if n == "manifest.json":
                m1 = json.loads(zf1.read(n).decode("utf-8"))
                m2 = json.loads(zf2.read(n).decode("utf-8"))
                for k in ("batch_id", "batch_status", "config_digest_sha256", "counts"):
                    assert m1.get(k) == m2.get(k), f"manifest[{k}] 不一致"
                for fname, sha in m1["files_sha256"].items():
                    if fname != "manifest.json" and fname != "README.txt":
                        assert m2["files_sha256"].get(fname) == sha, f"{fname} SHA 不一致"
            elif n == "README.txt":
                pass
            else:
                assert zf1.read(n) == zf2.read(n), f"{n} 内容不一致"


# ---------------------------------------------------------------------------
# 7. audit-pack: 同名输出冲突
# ---------------------------------------------------------------------------

def test_audit_pack_output_conflict(runner, fresh_env):
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
    bid = Storage(env["storage_dir"]).list_batches()[0].batch_id

    out_zip = env["tmp_path"] / "audit.zip"
    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "audit-pack", "--batch-id", bid,
            "--output", str(out_zip),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS
    first_sha = compute_sha256(out_zip)

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "audit-pack", "--batch-id", bid,
            "--output", str(out_zip),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.AUDIT_OUTPUT_CONFLICT, f"got {r.exit_code}, stdout={r.stdout}"
    assert "已存在" in (r.stdout + (r.stderr or ""))
    assert compute_sha256(out_zip) == first_sha

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "audit-pack", "--batch-id", bid,
            "--output", str(out_zip), "--force",
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS


# ---------------------------------------------------------------------------
# 8. audit-pack: 不可写目录
# ---------------------------------------------------------------------------

def test_audit_pack_unwritable_dir(runner, fresh_env, monkeypatch):
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
    bid = Storage(env["storage_dir"]).list_batches()[0].batch_id

    unwritable = env["tmp_path"] / "readonly_dir"
    unwritable.mkdir()
    import stat
    readonly = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
    try:
        os.chmod(unwritable, readonly)
    except (PermissionError, OSError):
        pytest.skip("当前系统无法设置目录只读权限（可能为 Windows 管理员）")

    probe = unwritable / ".probe_write"
    try:
        probe.write_bytes(b"x")
        probe.unlink()
        pytest.skip("当前平台 chmod 对目录不生效，跳过不可写目录测试")
    except (PermissionError, OSError):
        pass

    out_zip = unwritable / "audit.zip"
    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "audit-pack", "--batch-id", bid,
            "--output", str(out_zip),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code in (ExitCode.AUDIT_OUTPUT_PERMISSION, ExitCode.IO_ERROR), \
        f"got {r.exit_code}, stdout={r.stdout}"
    assert not out_zip.exists()

    os.chmod(unwritable, stat.S_IRWXU)


# ---------------------------------------------------------------------------
# 9. audit-pack: 批次不存在 / pending 状态 / 缺少报告
# ---------------------------------------------------------------------------

def test_audit_pack_batch_not_found(runner, fresh_env):
    env = fresh_env
    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "audit-pack", "--batch-id", "no-such-batch",
            "--output", str(env["tmp_path"] / "x.zip"),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.BATCH_NOT_FOUND, f"got {r.exit_code}, stdout={r.stdout}"


def test_audit_pack_pending_status_rejected(runner, fresh_env):
    env = fresh_env
    storage = Storage(env["storage_dir"])
    batch = storage.create_batch("pending-batch-001")
    assert batch.status == BatchStatus.PENDING

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "audit-pack", "--batch-id", "pending-batch-001",
            "--output", str(env["tmp_path"] / "x.zip"),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.AUDIT_INVALID_BATCH_STATUS, \
        f"got {r.exit_code}, stdout={r.stdout}"
    assert "pending" in (r.stdout + (r.stderr or ""))


# ---------------------------------------------------------------------------
# 10. audit-verify: 归档被改动（篡改 SHA、删文件、改内容）
# ---------------------------------------------------------------------------

def test_audit_verify_detects_tampered_file(runner, fresh_env):
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
    bid = Storage(env["storage_dir"]).list_batches()[0].batch_id
    r = runner.invoke(
        main,
        ["--storage-dir", str(env["storage_dir"]), "dispatch", "--batch-id", bid],
        catch_exceptions=False,
    )

    out_zip = env["tmp_path"] / "audit.zip"
    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "audit-pack", "--batch-id", bid,
            "--output", str(out_zip),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS

    tampered = env["tmp_path"] / "audit_tampered.zip"
    with zipfile.ZipFile(out_zip, "r") as zin, zipfile.ZipFile(tampered, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "precheck_report.json":
                rep = json.loads(data.decode("utf-8"))
                rep["total_rows"] = 99999
                data = json.dumps(rep, ensure_ascii=False, indent=2).encode("utf-8")
            zout.writestr(item, data)

    r = runner.invoke(
        main,
        ["audit-verify", "--archive", str(tampered)],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.AUDIT_VERIFY_FAILED, f"got {r.exit_code}, stdout={r.stdout}"
    assert "校验失败" in (r.stdout + (r.stderr or ""))
    assert "SHA256" in (r.stdout + (r.stderr or ""))


def test_audit_verify_detects_missing_file(runner, fresh_env):
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
    bid = Storage(env["storage_dir"]).list_batches()[0].batch_id

    out_zip = env["tmp_path"] / "audit.zip"
    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "audit-pack", "--batch-id", bid,
            "--output", str(out_zip),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS

    missing = env["tmp_path"] / "audit_missing.zip"
    with zipfile.ZipFile(out_zip, "r") as zin, zipfile.ZipFile(missing, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            if item.filename == "config_snapshot.json":
                continue
            zout.writestr(item, zin.read(item.filename))

    r = runner.invoke(
        main,
        ["audit-verify", "--archive", str(missing)],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.AUDIT_VERIFY_FAILED, f"got {r.exit_code}, stdout={r.stdout}"
    assert "缺失" in (r.stdout + (r.stderr or ""))


def test_audit_verify_non_zip(runner, fresh_env):
    env = fresh_env
    bad = env["tmp_path"] / "not_a_zip.zip"
    bad.write_bytes(b"this is not a zip file at all")
    r = runner.invoke(
        main,
        ["audit-verify", "--archive", str(bad)],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.AUDIT_VERIFY_FAILED
    assert not ("[未处理异常]" in (r.stdout + (r.stderr or "")))


def test_audit_pack_no_half_file_on_error(runner, fresh_env, monkeypatch):
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
    bid = Storage(env["storage_dir"]).list_batches()[0].batch_id

    target = env["tmp_path"] / "audit_fail.zip"

    from exam_paper_dispatcher import audit_pack as ap

    real_zipfile_zipfile = zipfile.ZipFile

    def bad_writestr(self, *args, **kwargs):
        raise OSError("simulated writestr failure")

    monkeypatch.setattr(ap.zipfile.ZipFile, "writestr", bad_writestr)

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "audit-pack", "--batch-id", bid,
            "--output", str(target),
        ],
    )
    assert r.exit_code != ExitCode.SUCCESS, f"got {r.exit_code}, stdout={r.stdout}"
    assert not target.exists()
    for leftover in env["tmp_path"].glob("audit_*.tmp"):
        leftover.unlink()
        assert False, f"存在临时半截文件: {leftover}"
