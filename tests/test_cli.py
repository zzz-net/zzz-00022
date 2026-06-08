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
README_PATH = REPO_ROOT / "README.md"


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


# ---------------------------------------------------------------------------
# 11. README 文档回归：确保审计命令和退出码不被文档遗漏
# ---------------------------------------------------------------------------

def test_readme_mentions_audit_commands_and_exit_codes():
    assert README_PATH.exists(), "README.md 不存在"
    readme = README_PATH.read_text(encoding="utf-8")

    required_strings = [
        "audit-pack",
        "audit-verify",
        "20",
        "21",
        "22",
        "23",
        "24",
        "AUDIT_OUTPUT_CONFLICT",
        "AUDIT_OUTPUT_PERMISSION",
        "AUDIT_MISSING_REPORT",
        "AUDIT_INVALID_BATCH_STATUS",
        "AUDIT_VERIFY_FAILED",
    ]
    missing = [s for s in required_strings if s not in readme]
    assert not missing, f"README 缺少以下关键字段: {missing}"


def test_readme_commands_match_cli_help(runner):
    readme = README_PATH.read_text(encoding="utf-8")

    r = runner.invoke(main, ["audit-pack", "--help"])
    help_text = r.stdout + (r.stderr or "")
    assert r.exit_code == 0
    for keyword in ("batch-id", "output", "force"):
        assert keyword in help_text, f"audit-pack --help 缺少选项: {keyword}"

    r = runner.invoke(main, ["audit-verify", "--help"])
    help_text = r.stdout + (r.stderr or "")
    assert r.exit_code == 0
    assert "archive" in help_text, "audit-verify --help 缺少 --archive 选项"

    r = runner.invoke(main, ["--help"])
    top_help = r.stdout + (r.stderr or "")
    assert "audit-pack" in top_help, "顶层 --help 未列出 audit-pack"
    assert "audit-verify" in top_help, "顶层 --help 未列出 audit-verify"


def test_readme_exit_codes_match_models():
    readme = README_PATH.read_text(encoding="utf-8")

    audit_exit_codes = [
        (ExitCode.AUDIT_OUTPUT_CONFLICT, "AUDIT_OUTPUT_CONFLICT"),
        (ExitCode.AUDIT_OUTPUT_PERMISSION, "AUDIT_OUTPUT_PERMISSION"),
        (ExitCode.AUDIT_MISSING_REPORT, "AUDIT_MISSING_REPORT"),
        (ExitCode.AUDIT_INVALID_BATCH_STATUS, "AUDIT_INVALID_BATCH_STATUS"),
        (ExitCode.AUDIT_VERIFY_FAILED, "AUDIT_VERIFY_FAILED"),
    ]
    for code, name in audit_exit_codes:
        assert str(code) in readme, f"README 缺少退出码 {code} ({name})"
        assert name in readme, f"README 缺少退出码常量名 {name}"


# ---------------------------------------------------------------------------
# 12. preview: 成功预演，不写出试卷包
# ---------------------------------------------------------------------------

def test_preview_success_no_output(runner, fresh_env):
    env = fresh_env

    assert not any(env["output_dir"].iterdir()), "预演前输出目录应为空"

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "preview",
            "--config", str(env["config_path"]),
            "--rooms", str(env["rooms_path"]),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert "预演" in r.stdout
    assert "源文件根目录" in r.stdout
    assert "输出目录" in r.stdout
    assert "预演明细" in r.stdout
    assert "预演通过" in r.stdout

    assert not any(env["output_dir"].iterdir()), "预演后输出目录仍应为空"

    storage = Storage(env["storage_dir"])
    batches = storage.list_batches()
    assert len(batches) == 1
    batch = batches[0]
    assert batch.status == BatchStatus.PREVIEW

    preview_ids = batch.list_preview_ids()
    assert len(preview_ids) == 1

    rpt = batch.load_preview_report(preview_ids[0])
    assert rpt is not None
    assert rpt.batch_id == batch.batch_id
    assert rpt.total_rows == 3
    assert rpt.valid_rows == 3
    assert len(rpt.missing_sources) == 0
    assert len(rpt.target_conflicts) == 0
    assert len(rpt.invalid_subjects) == 0
    assert len(rpt.invalid_versions) == 0
    assert len(rpt.preview_items) == 3
    assert len(rpt.potential_conflicts) == 0
    assert rpt.passed is True

    for item in rpt.preview_items:
        assert item["source_exists"] is True
        assert item["target_already_exists"] is False
        assert item["version"]
        assert item["source_sha256"]


# ---------------------------------------------------------------------------
# 13. preview: 冲突预演（缺失源文件 + 目标名冲突）
# ---------------------------------------------------------------------------

def test_preview_with_conflicts(runner, fresh_env):
    env = fresh_env
    bad_rooms = env["tmp_path"] / "bad_preview.csv"
    bad_rooms.write_text(
        "exam_id,room_id,subject,students_count,source_file,target_name\n"
        "20260608,A101,math,30,MISSING_FILE.pdf,20260608_A101_math\n"
        "20260608,A102,math,28,paper_math_v1.pdf,DUPLICATE_TARGET\n"
        "20260608,A103,math,25,paper_math_v1.pdf,DUPLICATE_TARGET\n"
        "20260608,B201,physics,30,paper_math_v1.pdf,20260608_B201_physics\n",
        encoding="utf-8",
    )

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "preview",
            "--config", str(env["config_path"]),
            "--rooms", str(bad_rooms),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert "预演存在问题" in r.stdout
    assert "缺失源文件" in r.stdout
    assert "目标文件名冲突" in r.stdout
    assert "DUPLICATE_TARGET" in r.stdout
    assert "非法科目或人数" in r.stdout

    storage = Storage(env["storage_dir"])
    batch = storage.list_batches()[0]
    rpt = batch.load_preview_report(batch.list_preview_ids()[0])
    assert rpt.passed is False
    assert len(rpt.missing_sources) == 1
    assert len(rpt.target_conflicts) == 1
    assert len(rpt.invalid_subjects) >= 1
    assert len(rpt.warnings) >= 3


# ---------------------------------------------------------------------------
# 14. preview: 同一 batch-id 重复预演，不覆盖旧记录
# ---------------------------------------------------------------------------

def test_preview_same_batch_id_no_overwrite(runner, fresh_env):
    env = fresh_env

    r1 = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "preview",
            "--config", str(env["config_path"]),
            "--rooms", str(env["rooms_path"]),
            "--batch-id", "preview-batch-001",
        ],
        catch_exceptions=False,
    )
    assert r1.exit_code == ExitCode.SUCCESS

    batch1 = Storage(env["storage_dir"]).get_batch("preview-batch-001")
    first_preview_ids = batch1.list_preview_ids()
    assert len(first_preview_ids) == 1
    first_rpt = batch1.load_preview_report(first_preview_ids[0])
    first_created = batch1.created_at
    first_previewed_at = first_rpt.previewed_at

    other_rooms = env["tmp_path"] / "other_preview.csv"
    other_rooms.write_text(
        "exam_id,room_id,subject,students_count,source_file,target_name\n"
        "20260609,Z999,math,10,paper_math_v1.pdf,20260609_Z999_math\n",
        encoding="utf-8",
    )

    import time
    time.sleep(0.05)

    r2 = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "preview",
            "--config", str(env["config_path"]),
            "--rooms", str(other_rooms),
            "--batch-id", "preview-batch-001",
        ],
        catch_exceptions=False,
    )
    assert r2.exit_code == ExitCode.SUCCESS, f"stdout={r2.stdout}\nstderr={r2.stderr}"
    assert "追加预演" in r2.stdout

    batch2 = Storage(env["storage_dir"]).get_batch("preview-batch-001")
    second_preview_ids = batch2.list_preview_ids()
    assert len(second_preview_ids) == 2
    assert first_preview_ids[0] in second_preview_ids

    first_still = batch2.load_preview_report(first_preview_ids[0])
    assert first_still.total_rows == 3
    assert first_still.previewed_at == first_previewed_at

    all_reports = batch2.load_all_preview_reports()
    assert len(all_reports) == 2
    other_rpts = [r for r in all_reports if r.csv_path == str(other_rooms)]
    assert len(other_rpts) == 1
    assert other_rpts[0].total_rows == 1

    assert batch2.created_at == first_created


# ---------------------------------------------------------------------------
# 15. preview: 跨重启查询（重建 Storage 后仍能查到预演记录）
# ---------------------------------------------------------------------------

def test_preview_query_across_restart(runner, fresh_env):
    env = fresh_env

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "preview",
            "--config", str(env["config_path"]),
            "--rooms", str(env["rooms_path"]),
            "--batch-id", "restart-preview",
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS

    del r

    storage_after = Storage(env["storage_dir"])
    batch = storage_after.get_batch("restart-preview")
    assert batch is not None
    assert batch.status == BatchStatus.PREVIEW
    preview_ids = batch.list_preview_ids()
    assert len(preview_ids) == 1

    rpt = batch.load_preview_report(preview_ids[0])
    assert rpt is not None
    assert rpt.total_rows == 3
    assert rpt.valid_rows == 3
    assert len(rpt.preview_items) == 3

    r_list = runner.invoke(
        main,
        ["--storage-dir", str(env["storage_dir"]), "query"],
        catch_exceptions=False,
    )
    assert r_list.exit_code == ExitCode.SUCCESS
    listed_batches = storage_after.list_batches()
    assert any(b.batch_id == "restart-preview" for b in listed_batches)
    assert any(b.status == BatchStatus.PREVIEW for b in listed_batches)

    r_detail = runner.invoke(
        main,
        ["--storage-dir", str(env["storage_dir"]), "query", "--batch-id", "restart-preview"],
        catch_exceptions=False,
    )
    assert r_detail.exit_code == ExitCode.SUCCESS
    detail = json.loads(r_detail.stdout)
    assert detail["batch_id"] == "restart-preview"
    assert "previews" in detail
    assert detail["previews"]["count"] == 1
    assert len(detail["previews"]["summary"]) == 1
    assert detail["previews"]["summary"][0]["total_rows"] == 3
    assert detail["previews"]["summary"][0]["preview_items_count"] == 3


# ---------------------------------------------------------------------------
# 16. preview: 导出 JSON/CSV 内容包含预演摘要且跨重启一致
# ---------------------------------------------------------------------------

def test_preview_export_consistency(runner, fresh_env):
    env = fresh_env

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "preview",
            "--config", str(env["config_path"]),
            "--rooms", str(env["rooms_path"]),
            "--batch-id", "export-preview",
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS

    other_rooms = env["tmp_path"] / "extra.csv"
    other_rooms.write_text(
        "exam_id,room_id,subject,students_count,source_file,target_name\n"
        "20260610,C301,english,20,paper_english_v2.pdf,20260610_C301_english\n",
        encoding="utf-8",
    )
    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "preview",
            "--config", str(env["config_path"]),
            "--rooms", str(other_rooms),
            "--batch-id", "export-preview",
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS

    out_json1 = env["tmp_path"] / "export_v1.json"
    out_csv1 = env["tmp_path"] / "batches_v1.csv"
    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "export", "--format", "json", "--output", str(out_json1),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS
    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "export", "--format", "csv", "--output", str(out_csv1),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS

    del r

    Storage2 = Storage
    storage2 = Storage2(env["storage_dir"])
    batch2 = storage2.get_batch("export-preview")
    assert len(batch2.list_preview_ids()) == 2

    out_json2 = env["tmp_path"] / "export_v2.json"
    out_csv2 = env["tmp_path"] / "batches_v2.csv"
    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "export", "--format", "json", "--output", str(out_json2),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS
    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "export", "--format", "csv", "--output", str(out_csv2),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS

    data1 = json.loads(out_json1.read_text(encoding="utf-8"))
    data2 = json.loads(out_json2.read_text(encoding="utf-8"))
    assert len(data1["batches"]) == len(data2["batches"]) == 1
    b1 = data1["batches"][0]
    b2 = data2["batches"][0]
    assert b1["batch_id"] == b2["batch_id"] == "export-preview"
    assert b1["previews"]["count"] == b2["previews"]["count"] == 2
    assert len(b1["previews"]["summary"]) == len(b2["previews"]["summary"]) == 2
    for i in range(2):
        for key in (
            "preview_id", "total_rows", "valid_rows",
            "missing_sources_count", "target_conflicts_count",
            "invalid_subjects_count", "invalid_versions_count",
            "potential_conflicts_count",
        ):
            assert b1["previews"]["summary"][i][key] == b2["previews"]["summary"][i][key]

    import csv as csv_mod
    with out_csv1.open("r", encoding="utf-8-sig") as f:
        rows1 = list(csv_mod.DictReader(f))
    with out_csv2.open("r", encoding="utf-8-sig") as f:
        rows2 = list(csv_mod.DictReader(f))
    assert len(rows1) == len(rows2) == 1
    assert rows1[0]["batch_id"] == rows2[0]["batch_id"] == "export-preview"
    assert rows1[0]["preview_count"] == rows2[0]["preview_count"] == "2"
    assert "preview_count" in rows1[0]
    assert "latest_preview_id" in rows1[0]
    assert rows1[0]["latest_preview_id"] == rows2[0]["latest_preview_id"]


# ---------------------------------------------------------------------------
# 17. README & CLI help 提及 preview 命令
# ---------------------------------------------------------------------------

def test_readme_mentions_preview():
    readme = README_PATH.read_text(encoding="utf-8")
    for keyword in ("preview", "导入预演", "预演"):
        assert keyword in readme, f"README 缺少关键字: {keyword}"


def test_cli_help_mentions_preview(runner):
    r = runner.invoke(main, ["--help"])
    top_help = r.stdout + (r.stderr or "")
    assert r.exit_code == 0
    assert "preview" in top_help

    r = runner.invoke(main, ["preview", "--help"])
    preview_help = r.stdout + (r.stderr or "")
    assert r.exit_code == 0
    for keyword in ("config", "rooms", "batch-id"):
        assert keyword in preview_help, f"preview --help 缺少选项: {keyword}"


# ---------------------------------------------------------------------------
# 18. signoff: 成功导入签收（发放后）
# ---------------------------------------------------------------------------

def _dispatch_successful_batch(runner, fresh_env):
    """辅助函数：完成一次完整的 precheck + dispatch，返回 batch_id。"""
    env = fresh_env
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
    assert r.exit_code == ExitCode.SUCCESS
    bid = Storage(env["storage_dir"]).list_batches()[0].batch_id
    r = runner.invoke(
        main,
        ["--storage-dir", str(env["storage_dir"]), "dispatch", "--batch-id", bid],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS
    return bid


def test_signoff_success_after_dispatch(runner, fresh_env):
    env = fresh_env
    bid = _dispatch_successful_batch(runner, env)

    signoffs_csv = env["tmp_path"] / "signoffs.csv"
    signoffs_csv.write_text(
        "exam_id,room_id,subject,signoff_person,signoff_time,received_count,damage_note,remark\n"
        "20260608,A101,math,张三,2026-06-08 09:00:00,30,,\n"
        "20260608,A102,math,李四,2026-06-08 09:05:00,28,第3份封面轻微破损,\n"
        "20260608,B201,english,王五,2026-06-08 09:10:00,35,,正常签收\n",
        encoding="utf-8",
    )

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "signoff",
            "--batch-id", bid,
            "--signoffs", str(signoffs_csv),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert "签收导入成功" in r.stdout
    assert "已签收考场: 3" in r.stdout
    assert "异常数: 1" in r.stdout

    batch = Storage(env["storage_dir"]).get_batch(bid)
    signoff_ids = batch.list_signoff_ids()
    assert len(signoff_ids) == 1
    rpt = batch.load_signoff_report(signoff_ids[0])
    assert rpt is not None
    assert rpt.passed is True
    assert rpt.signed_rooms == 3
    assert rpt.abnormal_count == 1
    assert rpt.total_rows == 3
    assert rpt.valid_rows == 3
    assert len(rpt.signoff_items) == 3

    abnormal_items = [it for it in rpt.signoff_items if it.get("is_abnormal")]
    assert len(abnormal_items) == 1
    assert abnormal_items[0]["room_id"] == "A102"
    assert abnormal_items[0]["damage_note"] == "第3份封面轻微破损"


# ---------------------------------------------------------------------------
# 19. signoff: 批次未发放不可签收
# ---------------------------------------------------------------------------

def test_signoff_rejected_before_dispatch(runner, fresh_env):
    env = fresh_env
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
    assert r.exit_code == ExitCode.SUCCESS
    bid = Storage(env["storage_dir"]).list_batches()[0].batch_id

    signoffs_csv = env["tmp_path"] / "signoffs.csv"
    signoffs_csv.write_text(
        "exam_id,room_id,subject,signoff_person,signoff_time,received_count\n"
        "20260608,A101,math,张三,2026-06-08 09:00:00,30\n",
        encoding="utf-8",
    )

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "signoff", "--batch-id", bid,
            "--signoffs", str(signoffs_csv),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SIGNOFF_BATCH_NOT_DISPATCHED, \
        f"got {r.exit_code}, stdout={r.stdout}"
    combined = r.stdout + (r.stderr or "")
    assert ("完成发放" in combined) or ("completed" in combined)

    batch = Storage(env["storage_dir"]).get_batch(bid)
    assert len(batch.list_signoff_ids()) == 0


# ---------------------------------------------------------------------------
# 20. signoff: 考场不在批次中 + 份数不匹配
# ---------------------------------------------------------------------------

def test_signoff_invalid_room_and_count_mismatch(runner, fresh_env):
    env = fresh_env
    bid = _dispatch_successful_batch(runner, env)

    signoffs_csv = env["tmp_path"] / "signoffs_bad.csv"
    signoffs_csv.write_text(
        "exam_id,room_id,subject,signoff_person,signoff_time,received_count\n"
        "20260608,Z999,math,张三,2026-06-08 09:00:00,30\n"
        "20260608,A101,math,李四,2026-06-08 09:05:00,999\n",
        encoding="utf-8",
    )

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "signoff", "--batch-id", bid,
            "--signoffs", str(signoffs_csv),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code in (
        ExitCode.SIGNOFF_ROOM_NOT_IN_BATCH,
        ExitCode.SIGNOFF_COUNT_MISMATCH,
        ExitCode.SIGNOFF_CONFLICT,
    ), f"got {r.exit_code}, stdout={r.stdout}"
    assert "签收导入失败" in r.stdout
    assert "考场不在批次" in (r.stdout + (r.stderr or ""))
    assert "份数不匹配" in (r.stdout + (r.stderr or ""))

    batch = Storage(env["storage_dir"]).get_batch(bid)
    assert len(batch.list_signoff_ids()) == 0


# ---------------------------------------------------------------------------
# 21. signoff: 重复导入冲突需 --force
# ---------------------------------------------------------------------------

def test_signoff_conflict_without_force_then_force_update(runner, fresh_env):
    env = fresh_env
    bid = _dispatch_successful_batch(runner, env)

    signoffs_csv_1 = env["tmp_path"] / "signoffs_v1.csv"
    signoffs_csv_1.write_text(
        "exam_id,room_id,subject,signoff_person,signoff_time,received_count\n"
        "20260608,A101,math,张三,2026-06-08 09:00:00,30\n"
        "20260608,A102,math,李四,2026-06-08 09:05:00,28\n",
        encoding="utf-8",
    )

    r1 = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "signoff", "--batch-id", bid,
            "--signoffs", str(signoffs_csv_1),
        ],
        catch_exceptions=False,
    )
    assert r1.exit_code == ExitCode.SUCCESS

    batch_before = Storage(env["storage_dir"]).get_batch(bid)
    ids_before = batch_before.list_signoff_ids()
    assert len(ids_before) == 1
    first_rpt = batch_before.load_signoff_report(ids_before[0])
    item_a101_first = next(
        it for it in first_rpt.signoff_items
        if it["room_id"] == "A101"
    )
    assert item_a101_first["signoff_person"] == "张三"

    signoffs_csv_2 = env["tmp_path"] / "signoffs_v2.csv"
    signoffs_csv_2.write_text(
        "exam_id,room_id,subject,signoff_person,signoff_time,received_count\n"
        "20260608,A101,math,赵六,2026-06-08 10:00:00,30\n",
        encoding="utf-8",
    )

    r2 = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "signoff", "--batch-id", bid,
            "--signoffs", str(signoffs_csv_2),
        ],
        catch_exceptions=False,
    )
    assert r2.exit_code == ExitCode.SIGNOFF_UPDATE_WITHOUT_FORCE, \
        f"got {r2.exit_code}, stdout={r2.stdout}"
    assert "重复签收冲突" in (r2.stdout + (r2.stderr or ""))
    assert "force" in (r2.stdout + (r2.stderr or ""))

    batch_between = Storage(env["storage_dir"]).get_batch(bid)
    assert len(batch_between.list_signoff_ids()) == 1

    r3 = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "signoff", "--batch-id", bid,
            "--signoffs", str(signoffs_csv_2),
            "--force",
        ],
        catch_exceptions=False,
    )
    assert r3.exit_code == ExitCode.SUCCESS, f"stdout={r3.stdout}\nstderr={r3.stderr}"

    batch_after = Storage(env["storage_dir"]).get_batch(bid)
    ids_after = batch_after.list_signoff_ids()
    assert len(ids_after) == 2
    latest = batch_after.load_latest_signoff_report()
    assert latest is not None
    item_a101_latest = next(
        it for it in latest.signoff_items
        if it["room_id"] == "A101"
    )
    assert item_a101_latest["signoff_person"] == "赵六"


# ---------------------------------------------------------------------------
# 22. signoff: 跨重启查询持久性
# ---------------------------------------------------------------------------

def test_signoff_query_across_restart(runner, fresh_env):
    env = fresh_env
    bid = _dispatch_successful_batch(runner, env)

    signoffs_csv = env["tmp_path"] / "signoffs.csv"
    signoffs_csv.write_text(
        "exam_id,room_id,subject,signoff_person,signoff_time,received_count\n"
        "20260608,A101,math,张三,2026-06-08 09:00:00,30\n"
        "20260608,A102,math,李四,2026-06-08 09:05:00,28\n",
        encoding="utf-8",
    )

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "signoff", "--batch-id", bid,
            "--signoffs", str(signoffs_csv),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS

    del r

    storage_after = Storage(env["storage_dir"])
    batch = storage_after.get_batch(bid)
    assert batch is not None
    signoff_ids = batch.list_signoff_ids()
    assert len(signoff_ids) == 1
    rpt = batch.load_signoff_report(signoff_ids[0])
    assert rpt is not None
    assert rpt.signed_rooms == 2

    r_list = runner.invoke(
        main,
        ["--storage-dir", str(env["storage_dir"]), "query"],
        catch_exceptions=False,
    )
    assert r_list.exit_code == ExitCode.SUCCESS
    combined = r_list.stdout + (r_list.stderr or "")
    stripped = combined.replace("\n", "").replace(" ", "").replace("\r", "")
    assert ("签收" in stripped) or ("partial" in stripped)

    r_detail = runner.invoke(
        main,
        ["--storage-dir", str(env["storage_dir"]), "query", "--batch-id", bid],
        catch_exceptions=False,
    )
    assert r_detail.exit_code == ExitCode.SUCCESS
    detail = json.loads(r_detail.stdout)
    assert detail["batch_id"] == bid
    assert "signoff" in detail
    assert detail["signoff"]["has_signoff"] is True
    assert detail["signoff"]["count"] == 1
    assert detail["signoff"]["status"] == "partial"
    assert detail["signoff"]["signed_rooms"] == 2
    assert detail["signoff"]["total_expected"] == 3
    assert detail["signoff"]["last_imported_at"] is not None


# ---------------------------------------------------------------------------
# 23. signoff: 导出 JSON/CSV 内容一致性（跨重启）
# ---------------------------------------------------------------------------

def test_signoff_export_consistency(runner, fresh_env):
    env = fresh_env
    bid = _dispatch_successful_batch(runner, env)

    signoffs_csv = env["tmp_path"] / "signoffs.csv"
    signoffs_csv.write_text(
        "exam_id,room_id,subject,signoff_person,signoff_time,received_count,damage_note\n"
        "20260608,A101,math,张三,2026-06-08 09:00:00,30,\n"
        "20260608,A102,math,李四,2026-06-08 09:05:00,28,破损\n"
        "20260608,B201,english,王五,2026-06-08 09:10:00,35,\n",
        encoding="utf-8",
    )

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "signoff", "--batch-id", bid,
            "--signoffs", str(signoffs_csv),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS

    out_json1 = env["tmp_path"] / "export_json_v1.json"
    out_csv1 = env["tmp_path"] / "export_batches_v1.csv"
    out_items1 = env["tmp_path"] / "export_items_v1.csv"
    for fmt, path in (
        ("json", out_json1),
        ("csv", out_csv1),
    ):
        r = runner.invoke(
            main,
            [
                "--storage-dir", str(env["storage_dir"]),
                "export", "--format", fmt, "--output", str(path),
            ],
            catch_exceptions=False,
        )
        assert r.exit_code == ExitCode.SUCCESS
    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "export", "--format", "csv-items",
            "--batch-id", bid, "--output", str(out_items1),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS

    del r

    Storage2 = Storage
    storage2 = Storage2(env["storage_dir"])
    batch2 = storage2.get_batch(bid)
    assert len(batch2.list_signoff_ids()) == 1

    out_json2 = env["tmp_path"] / "export_json_v2.json"
    out_csv2 = env["tmp_path"] / "export_batches_v2.csv"
    out_items2 = env["tmp_path"] / "export_items_v2.csv"
    for fmt, path in (
        ("json", out_json2),
        ("csv", out_csv2),
    ):
        r = runner.invoke(
            main,
            [
                "--storage-dir", str(env["storage_dir"]),
                "export", "--format", fmt, "--output", str(path),
            ],
            catch_exceptions=False,
        )
        assert r.exit_code == ExitCode.SUCCESS
    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "export", "--format", "csv-items",
            "--batch-id", bid, "--output", str(out_items2),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS

    data1 = json.loads(out_json1.read_text(encoding="utf-8"))
    data2 = json.loads(out_json2.read_text(encoding="utf-8"))
    assert len(data1["batches"]) == len(data2["batches"]) == 1
    b1 = data1["batches"][0]
    b2 = data2["batches"][0]
    assert b1["batch_id"] == b2["batch_id"] == bid
    assert b1["signoff"]["has_signoff"] == b2["signoff"]["has_signoff"] == True
    assert b1["signoff"]["count"] == b2["signoff"]["count"] == 1
    assert b1["signoff"]["status"] == b2["signoff"]["status"] == "complete"
    assert b1["signoff"]["signed_rooms"] == b2["signoff"]["signed_rooms"] == 3
    assert b1["signoff"]["abnormal_count"] == b2["signoff"]["abnormal_count"] == 1
    assert b1["signoff"]["last_imported_at"] == b2["signoff"]["last_imported_at"]

    import csv as csv_mod
    with out_csv1.open("r", encoding="utf-8-sig") as f:
        rows1 = list(csv_mod.DictReader(f))
    with out_csv2.open("r", encoding="utf-8-sig") as f:
        rows2 = list(csv_mod.DictReader(f))
    assert len(rows1) == len(rows2) == 1
    assert rows1[0]["batch_id"] == rows2[0]["batch_id"] == bid
    for key in (
        "signoff_count", "signoff_status", "signoff_signed_rooms",
        "signoff_abnormal_count", "signoff_last_imported_at",
    ):
        assert key in rows1[0], f"批次CSV缺少列: {key}"
        assert rows1[0][key] == rows2[0][key], f"{key} 不一致"

    with out_items1.open("r", encoding="utf-8-sig") as f:
        items1 = list(csv_mod.DictReader(f))
    with out_items2.open("r", encoding="utf-8-sig") as f:
        items2 = list(csv_mod.DictReader(f))
    assert len(items1) == len(items2) == 3
    for key in (
        "signed_off", "signoff_person", "signoff_time",
        "received_count", "damage_note", "signoff_abnormal",
    ):
        assert key in items1[0], f"发放明细CSV缺少列: {key}"
    signed_rows = [row for row in items1 if row["signed_off"] == "True"]
    assert len(signed_rows) == 3
    a102 = next(row for row in items1 if row["room_id"] == "A102")
    assert a102["signoff_person"] == "李四"
    assert a102["damage_note"] == "破损"
    assert a102["signoff_abnormal"] in ("True", "true", True)


# ---------------------------------------------------------------------------
# 24. signoff: 非法 CSV / 校验失败时不留下半成品
# ---------------------------------------------------------------------------

def test_signoff_invalid_csv_rejected_cleanly(runner, fresh_env):
    env = fresh_env
    bid = _dispatch_successful_batch(runner, env)

    bad_csv = env["tmp_path"] / "signoffs_bad_cols.csv"
    bad_csv.write_text(
        "bad_col1,bad_col2\n"
        "x,y\n",
        encoding="utf-8",
    )

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "signoff", "--batch-id", bid,
            "--signoffs", str(bad_csv),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.INVALID_CSV, f"got {r.exit_code}, stdout={r.stdout}"
    assert "缺少必要列" in (r.stdout + (r.stderr or ""))

    batch = Storage(env["storage_dir"]).get_batch(bid)
    assert len(batch.list_signoff_ids()) == 0
    signoff_dir = batch.batch_dir / "signoffs"
    assert not signoff_dir.exists() or not any(signoff_dir.iterdir())


def test_signoff_io_error_no_half_file(runner, fresh_env, monkeypatch):
    env = fresh_env
    bid = _dispatch_successful_batch(runner, env)

    signoffs_csv = env["tmp_path"] / "signoffs.csv"
    signoffs_csv.write_text(
        "exam_id,room_id,subject,signoff_person,signoff_time,received_count\n"
        "20260608,A101,math,张三,2026-06-08 09:00:00,30\n",
        encoding="utf-8",
    )

    from exam_paper_dispatcher import storage as st_mod

    real_path_write_text = Path.write_text

    def bad_write_text(self, *args, **kwargs):
        if self.match("*signoff-*.json"):
            raise OSError("simulated disk full")
        return real_path_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", bad_write_text)

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "signoff", "--batch-id", bid,
            "--signoffs", str(signoffs_csv),
        ],
    )
    assert r.exit_code != ExitCode.SUCCESS, f"stdout={r.stdout}"

    batch = Storage(env["storage_dir"]).get_batch(bid)
    signoff_ids = batch.list_signoff_ids()
    assert len(signoff_ids) == 0


# ---------------------------------------------------------------------------
# 25. README & CLI help 提及 signoff 命令和退出码
# ---------------------------------------------------------------------------

def test_readme_mentions_signoff():
    readme = README_PATH.read_text(encoding="utf-8")
    for keyword in ("signoff", "签收", "核销"):
        assert keyword in readme, f"README 缺少关键字: {keyword}"


def test_readme_signoff_exit_codes_match_models():
    readme = README_PATH.read_text(encoding="utf-8")
    signoff_exit_codes = [
        (ExitCode.SIGNOFF_BATCH_NOT_DISPATCHED, "SIGNOFF_BATCH_NOT_DISPATCHED"),
        (ExitCode.SIGNOFF_ROOM_NOT_IN_BATCH, "SIGNOFF_ROOM_NOT_IN_BATCH"),
        (ExitCode.SIGNOFF_COUNT_MISMATCH, "SIGNOFF_COUNT_MISMATCH"),
        (ExitCode.SIGNOFF_CONFLICT, "SIGNOFF_CONFLICT"),
        (ExitCode.SIGNOFF_UPDATE_WITHOUT_FORCE, "SIGNOFF_UPDATE_WITHOUT_FORCE"),
    ]
    for code, name in signoff_exit_codes:
        assert str(code) in readme, f"README 缺少退出码 {code} ({name})"
        assert name in readme, f"README 缺少退出码常量名 {name}"


def test_cli_help_mentions_signoff(runner):
    r = runner.invoke(main, ["--help"])
    top_help = r.stdout + (r.stderr or "")
    assert r.exit_code == 0
    assert "signoff" in top_help

    r = runner.invoke(main, ["signoff", "--help"])
    signoff_help = r.stdout + (r.stderr or "")
    assert r.exit_code == 0
    for keyword in ("batch-id", "signoffs", "force"):
        assert keyword in signoff_help, f"signoff --help 缺少选项: {keyword}"


# ---------------------------------------------------------------------------
# 26. signoff-correct: 正常更正签收（单字段、多字段）
# ---------------------------------------------------------------------------

def _dispatch_and_signoff(runner, fresh_env):
    """辅助函数：完成 precheck + dispatch + signoff，返回 batch_id。"""
    bid = _dispatch_successful_batch(runner, fresh_env)
    signoffs_csv = fresh_env["tmp_path"] / "signoffs.csv"
    signoffs_csv.write_text(
        "exam_id,room_id,subject,signoff_person,signoff_time,received_count,damage_note,remark\n"
        "20260608,A101,math,张三,2026-06-08 09:00:00,30,,\n"
        "20260608,A102,math,李四,2026-06-08 09:05:00,28,,\n"
        "20260608,B201,english,王五,2026-06-08 09:10:00,35,,\n",
        encoding="utf-8",
    )
    r = runner.invoke(
        main,
        [
            "--storage-dir", str(fresh_env["storage_dir"]),
            "signoff", "--batch-id", bid,
            "--signoffs", str(signoffs_csv),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS, f"stdout={r.stdout}\nstderr={r.stderr}"
    return bid


def test_signoff_correct_single_field(runner, fresh_env):
    env = fresh_env
    bid = _dispatch_and_signoff(runner, env)

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "signoff-correct",
            "--batch-id", bid,
            "--exam-id", "20260608",
            "--room-id", "A101",
            "--subject", "math",
            "--operator", "李主任",
            "--reason", "签收人姓名录入错误",
            "--signoff-person", "赵六",
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert "签收更正成功" in r.stdout
    assert "张三" in r.stdout
    assert "赵六" in r.stdout

    batch = Storage(env["storage_dir"]).get_batch(bid)
    assert batch.status == BatchStatus.COMPLETED

    effective = batch.get_effective_signoff_items()
    a101 = effective[("20260608", "A101", "math")]
    assert a101["signoff_person"] == "赵六"
    assert a101["version"] == 1
    assert a101["last_operator"] == "李主任"

    audit_log = batch.load_signoff_audit_log()
    assert len(audit_log) == 1
    assert audit_log[0].action.value == "correct"
    assert audit_log[0].operator == "李主任"
    assert audit_log[0].old_values["signoff_person"] == "张三"
    assert audit_log[0].new_values["signoff_person"] == "赵六"
    assert audit_log[0].version_before == 0
    assert audit_log[0].version_after == 1
    assert audit_log[0].reason == "签收人姓名录入错误"


def test_signoff_correct_multiple_fields(runner, fresh_env):
    env = fresh_env
    bid = _dispatch_and_signoff(runner, env)

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "signoff-correct",
            "--batch-id", bid,
            "--exam-id", "20260608",
            "--room-id", "A102",
            "--subject", "math",
            "--operator", "王监考",
            "--reason", "补录缺损说明和备注",
            "--signoff-time", "2026-06-08 10:30:00",
            "--damage-note", "第3份封条轻微破损",
            "--remark", "已拍照留档",
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS, f"stdout={r.stdout}\nstderr={r.stderr}"

    batch = Storage(env["storage_dir"]).get_batch(bid)
    effective = batch.get_effective_signoff_items()
    a102 = effective[("20260608", "A102", "math")]
    assert a102["signoff_time"] == "2026-06-08 10:30:00"
    assert a102["damage_note"] == "第3份封条轻微破损"
    assert a102["remark"] == "已拍照留档"
    assert a102["is_abnormal"] is True
    assert a102["version"] == 1


def test_signoff_correct_twice_version_increment(runner, fresh_env):
    env = fresh_env
    bid = _dispatch_and_signoff(runner, env)

    for i, (person, reason) in enumerate([
        ("李四", "第一次更正"),
        ("王五", "第二次更正"),
    ]):
        r = runner.invoke(
            main,
            [
                "--storage-dir", str(env["storage_dir"]),
                "signoff-correct",
                "--batch-id", bid,
                "--exam-id", "20260608",
                "--room-id", "A101",
                "--subject", "math",
                "--operator", f"操作员{i+1}",
                "--reason", reason,
                "--signoff-person", person,
            ],
            catch_exceptions=False,
        )
        assert r.exit_code == ExitCode.SUCCESS

    batch = Storage(env["storage_dir"]).get_batch(bid)
    effective = batch.get_effective_signoff_items()
    a101 = effective[("20260608", "A101", "math")]
    assert a101["signoff_person"] == "王五"
    assert a101["version"] == 2

    audit_log = batch.load_signoff_audit_log()
    assert len(audit_log) == 2
    assert audit_log[0].version_after == 1
    assert audit_log[1].version_after == 2


# ---------------------------------------------------------------------------
# 27. signoff-revoke: 正常撤销 + 批次状态保持 completed
# ---------------------------------------------------------------------------

def test_signoff_revoke_keeps_batch_status(runner, fresh_env):
    env = fresh_env
    bid = _dispatch_and_signoff(runner, env)

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "signoff-revoke",
            "--batch-id", bid,
            "--exam-id", "20260608",
            "--room-id", "A101",
            "--subject", "math",
            "--operator", "主考",
            "--reason", "该考场试卷包错发，需要重新发放签收",
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert "签收撤销成功" in r.stdout

    batch = Storage(env["storage_dir"]).get_batch(bid)
    assert batch.status == BatchStatus.COMPLETED, "撤销签收不能改变批次发放状态"

    effective = batch.get_effective_signoff_items()
    a101 = effective[("20260608", "A101", "math")]
    assert a101["revoked"] is True
    assert a101["revoked_by"] == "主考"
    assert a101["revoke_reason"] == "该考场试卷包错发，需要重新发放签收"

    signoff_summary = batch.to_dict()
    from exam_paper_dispatcher.signoff import build_signoff_summary
    summary = build_signoff_summary(batch)
    assert summary["status"] == "partial"
    assert summary["signed_rooms"] == 2
    assert summary["revoked_count"] == 1

    audit_log = batch.load_signoff_audit_log()
    assert len(audit_log) == 1
    assert audit_log[0].action.value == "revoke"


# ---------------------------------------------------------------------------
# 28. signoff-correct/revoke: 错误场景（非法字段、不存在考场、未签收、缺少原因）
# ---------------------------------------------------------------------------

def test_signoff_correct_invalid_field(runner, fresh_env):
    env = fresh_env
    bid = _dispatch_and_signoff(runner, env)

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "signoff-correct",
            "--batch-id", bid,
            "--exam-id", "20260608",
            "--room-id", "A101",
            "--subject", "math",
            "--operator", "test",
            "--reason", "test",
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SIGNOFF_CORRECT_INVALID_FIELD, \
        f"got {r.exit_code}, stdout={r.stdout}"

    batch = Storage(env["storage_dir"]).get_batch(bid)
    assert len(batch.load_signoff_audit_log()) == 0


def test_signoff_correct_room_not_in_batch(runner, fresh_env):
    env = fresh_env
    bid = _dispatch_and_signoff(runner, env)

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "signoff-correct",
            "--batch-id", bid,
            "--exam-id", "20260608",
            "--room-id", "Z999",
            "--subject", "math",
            "--operator", "test",
            "--reason", "test",
            "--signoff-person", "new",
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SIGNOFF_CORRECT_ROOM_NOT_FOUND, \
        f"got {r.exit_code}, stdout={r.stdout}"
    assert "不属于批次" in (r.stdout + (r.stderr or ""))

    batch = Storage(env["storage_dir"]).get_batch(bid)
    assert len(batch.load_signoff_audit_log()) == 0


def test_signoff_revoke_not_signed_yet(runner, fresh_env):
    env = fresh_env
    bid = _dispatch_successful_batch(runner, env)

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "signoff-revoke",
            "--batch-id", bid,
            "--exam-id", "20260608",
            "--room-id", "A101",
            "--subject", "math",
            "--operator", "test",
            "--reason", "test",
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SIGNOFF_REVOKE_NOT_SIGNED, \
        f"got {r.exit_code}, stdout={r.stdout}"

    batch = Storage(env["storage_dir"]).get_batch(bid)
    signoff_dir = batch.batch_dir / "signoffs"
    if signoff_dir.exists():
        assert not any(signoff_dir.glob("signoff_audit_log*"))


def test_signoff_correct_missing_reason(runner, fresh_env):
    env = fresh_env
    bid = _dispatch_and_signoff(runner, env)

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "signoff-correct",
            "--batch-id", bid,
            "--exam-id", "20260608",
            "--room-id", "A101",
            "--subject", "math",
            "--operator", "test",
            "--reason", "",
            "--signoff-person", "new",
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SIGNOFF_AUDIT_MISSING_REASON, \
        f"got {r.exit_code}, stdout={r.stdout}"

    batch = Storage(env["storage_dir"]).get_batch(bid)
    assert len(batch.load_signoff_audit_log()) == 0


def test_signoff_correct_same_value_no_op(runner, fresh_env):
    env = fresh_env
    bid = _dispatch_and_signoff(runner, env)

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "signoff-correct",
            "--batch-id", bid,
            "--exam-id", "20260608",
            "--room-id", "A101",
            "--subject", "math",
            "--operator", "test",
            "--reason", "test",
            "--signoff-person", "张三",
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert "无需更正" in r.stdout

    batch = Storage(env["storage_dir"]).get_batch(bid)
    assert len(batch.load_signoff_audit_log()) == 0


# ---------------------------------------------------------------------------
# 29. signoff-revoke -> 再导入冲突
# ---------------------------------------------------------------------------

def test_signoff_revoke_then_reimport(runner, fresh_env):
    env = fresh_env
    bid = _dispatch_and_signoff(runner, env)

    r_rev = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "signoff-revoke",
            "--batch-id", bid,
            "--exam-id", "20260608",
            "--room-id", "A101",
            "--subject", "math",
            "--operator", "主考",
            "--reason", "错发，需要重新签收",
        ],
        catch_exceptions=False,
    )
    assert r_rev.exit_code == ExitCode.SUCCESS

    new_signoffs = env["tmp_path"] / "signoffs_v2.csv"
    new_signoffs.write_text(
        "exam_id,room_id,subject,signoff_person,signoff_time,received_count\n"
        "20260608,A101,math,新签收人,2026-06-08 11:00:00,30\n",
        encoding="utf-8",
    )

    r_import = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "signoff", "--batch-id", bid,
            "--signoffs", str(new_signoffs),
        ],
        catch_exceptions=False,
    )
    assert r_import.exit_code == ExitCode.SIGNOFF_UPDATE_WITHOUT_FORCE, \
        f"got {r_import.exit_code}, stdout={r_import.stdout}"
    assert "重复签收冲突" in (r_import.stdout + (r_import.stderr or ""))

    r_import_force = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "signoff", "--batch-id", bid,
            "--signoffs", str(new_signoffs),
            "--force",
        ],
        catch_exceptions=False,
    )
    assert r_import_force.exit_code == ExitCode.SUCCESS

    batch = Storage(env["storage_dir"]).get_batch(bid)
    effective = batch.get_effective_signoff_items()
    a101 = effective[("20260608", "A101", "math")]
    assert a101.get("revoked") is not True, "force 重新导入后应覆盖撤销状态"
    assert a101["signoff_person"] == "新签收人"


# ---------------------------------------------------------------------------
# 30. signoff-history: 查询签收历史
# ---------------------------------------------------------------------------

def test_signoff_history_query(runner, fresh_env):
    env = fresh_env
    bid = _dispatch_and_signoff(runner, env)

    runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "signoff-correct",
            "--batch-id", bid,
            "--exam-id", "20260608",
            "--room-id", "A101",
            "--subject", "math",
            "--operator", "李主任",
            "--reason", "更正",
            "--signoff-person", "赵六",
        ],
        catch_exceptions=False,
    )
    runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "signoff-revoke",
            "--batch-id", bid,
            "--exam-id", "20260608",
            "--room-id", "A102",
            "--subject", "math",
            "--operator", "主考",
            "--reason", "撤销",
        ],
        catch_exceptions=False,
    )

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "signoff-history",
            "--batch-id", bid,
            "--format", "json",
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS
    history = json.loads(r.stdout)
    assert len(history) == 3

    a101_hist = next(h for h in history if h["room_id"] == "A101")
    assert a101_hist["current_version"] == 1
    assert len(a101_hist["import_records"]) >= 1
    assert len(a101_hist["audit_records"]) == 1
    assert a101_hist["audit_records"][0]["action"] == "correct"

    a102_hist = next(h for h in history if h["room_id"] == "A102")
    assert a102_hist["revoked"] is True
    assert len(a102_hist["audit_records"]) == 1
    assert a102_hist["audit_records"][0]["action"] == "revoke"


# ---------------------------------------------------------------------------
# 31. 跨重启 query/export 一致性
# ---------------------------------------------------------------------------

def test_signoff_correct_revoke_export_consistency(runner, fresh_env):
    env = fresh_env
    bid = _dispatch_and_signoff(runner, env)

    runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "signoff-correct",
            "--batch-id", bid,
            "--exam-id", "20260608",
            "--room-id", "A101",
            "--subject", "math",
            "--operator", "李主任",
            "--reason", "更正签收人",
            "--signoff-person", "赵六",
        ],
        catch_exceptions=False,
    )
    runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "signoff-revoke",
            "--batch-id", bid,
            "--exam-id", "20260608",
            "--room-id", "A102",
            "--subject", "math",
            "--operator", "主考",
            "--reason", "撤销",
        ],
        catch_exceptions=False,
    )

    out_json1 = env["tmp_path"] / "export_before_restart.json"
    out_csv1 = env["tmp_path"] / "batches_before.csv"
    out_items1 = env["tmp_path"] / "items_before.csv"
    for fmt, path in (("json", out_json1), ("csv", out_csv1)):
        r = runner.invoke(
            main,
            [
                "--storage-dir", str(env["storage_dir"]),
                "export", "--format", fmt, "--output", str(path),
            ],
            catch_exceptions=False,
        )
        assert r.exit_code == ExitCode.SUCCESS
    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "export", "--format", "csv-items",
            "--batch-id", bid, "--output", str(out_items1),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS

    del r
    Storage2 = Storage
    storage2 = Storage2(env["storage_dir"])
    batch2 = storage2.get_batch(bid)
    assert len(batch2.load_signoff_audit_log()) == 2

    out_json2 = env["tmp_path"] / "export_after_restart.json"
    out_csv2 = env["tmp_path"] / "batches_after.csv"
    out_items2 = env["tmp_path"] / "items_after.csv"
    for fmt, path in (("json", out_json2), ("csv", out_csv2)):
        r = runner.invoke(
            main,
            [
                "--storage-dir", str(env["storage_dir"]),
                "export", "--format", fmt, "--output", str(path),
            ],
            catch_exceptions=False,
        )
        assert r.exit_code == ExitCode.SUCCESS
    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "export", "--format", "csv-items",
            "--batch-id", bid, "--output", str(out_items2),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS

    data1 = json.loads(out_json1.read_text(encoding="utf-8"))
    data2 = json.loads(out_json2.read_text(encoding="utf-8"))
    b1 = data1["batches"][0]
    b2 = data2["batches"][0]
    assert b1["signoff"]["corrected_count"] == b2["signoff"]["corrected_count"] == 1
    assert b1["signoff"]["revoked_count"] == b2["signoff"]["revoked_count"] == 1
    assert b1["signoff"]["audit_count"] == b2["signoff"]["audit_count"] == 2
    assert b1["signoff"]["status"] == b2["signoff"]["status"] == "partial"
    assert b1["signoff_audit_log"] == b2["signoff_audit_log"]

    import csv as csv_mod
    with out_items1.open("r", encoding="utf-8-sig") as f:
        rows1 = list(csv_mod.DictReader(f))
    with out_items2.open("r", encoding="utf-8-sig") as f:
        rows2 = list(csv_mod.DictReader(f))
    a101_1 = next(r for r in rows1 if r["room_id"] == "A101")
    a101_2 = next(r for r in rows2 if r["room_id"] == "A101")
    assert a101_1["signoff_person"] == a101_2["signoff_person"] == "赵六"
    assert a101_1["signoff_version"] == a101_2["signoff_version"] == "1"
    a102_1 = next(r for r in rows1 if r["room_id"] == "A102")
    a102_2 = next(r for r in rows2 if r["room_id"] == "A102")
    assert a102_1["signoff_revoked"] == a102_2["signoff_revoked"] == "True"


# ---------------------------------------------------------------------------
# 32. audit-pack 包含更正撤销审计日志
# ---------------------------------------------------------------------------

def test_audit_pack_includes_signoff_audit(runner, fresh_env):
    env = fresh_env
    bid = _dispatch_and_signoff(runner, env)

    runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "signoff-correct",
            "--batch-id", bid,
            "--exam-id", "20260608",
            "--room-id", "A101",
            "--subject", "math",
            "--operator", "李主任",
            "--reason", "更正",
            "--signoff-person", "赵六",
        ],
        catch_exceptions=False,
    )

    out_zip = env["tmp_path"] / "audit_correct.zip"
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
        assert "signoff_audit_log.jsonl" in names
        assert "signoff_room_versions.json" in names
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert manifest["counts"]["signoff_corrected"] == 1
        assert manifest["counts"]["signoff_audit_total"] == 1
        readme = zf.read("README.txt").decode("utf-8")
        assert "签收更正次数" in readme
        audit_lines = zf.read("signoff_audit_log.jsonl").decode("utf-8").strip().splitlines()
        assert len(audit_lines) == 1
        audit_entry = json.loads(audit_lines[0])
        assert audit_entry["action"] == "correct"
        assert audit_entry["new_values"]["signoff_person"] == "赵六"


# ---------------------------------------------------------------------------
# 33. I/O 错误时不留下半截审计文件
# ---------------------------------------------------------------------------

def test_signoff_correct_io_error_no_half_file(runner, fresh_env, monkeypatch):
    env = fresh_env
    bid = _dispatch_and_signoff(runner, env)

    from exam_paper_dispatcher import storage as st_mod

    real_append = None

    def bad_append(self, *args, **kwargs):
        raise OSError("simulated disk full")

    monkeypatch.setattr(st_mod.BatchState, "append_signoff_audit", bad_append)

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "signoff-correct",
            "--batch-id", bid,
            "--exam-id", "20260608",
            "--room-id", "A101",
            "--subject", "math",
            "--operator", "test",
            "--reason", "test",
            "--signoff-person", "new",
        ],
    )
    assert r.exit_code != ExitCode.SUCCESS, f"stdout={r.stdout}"

    batch = Storage(env["storage_dir"]).get_batch(bid)
    audit_log = batch.load_signoff_audit_log()
    assert len(audit_log) == 0

    effective = batch.get_effective_signoff_items()
    a101 = effective[("20260608", "A101", "math")]
    assert a101["signoff_person"] == "张三"


# ---------------------------------------------------------------------------
# 34. README & CLI help 提及更正撤销命令和退出码
# ---------------------------------------------------------------------------

def test_readme_mentions_signoff_correct_revoke():
    readme = README_PATH.read_text(encoding="utf-8")
    for keyword in (
        "signoff-correct", "signoff-revoke", "signoff-history",
        "更正签收", "撤销签收", "签收历史", "审计日志",
    ):
        assert keyword in readme, f"README 缺少关键字: {keyword}"


def test_readme_correct_revoke_exit_codes_match_models():
    readme = README_PATH.read_text(encoding="utf-8")
    exit_codes = [
        (ExitCode.SIGNOFF_CORRECT_INVALID_FIELD, "SIGNOFF_CORRECT_INVALID_FIELD"),
        (ExitCode.SIGNOFF_CORRECT_ROOM_NOT_FOUND, "SIGNOFF_CORRECT_ROOM_NOT_FOUND"),
        (ExitCode.SIGNOFF_CORRECT_NOT_SIGNED, "SIGNOFF_CORRECT_NOT_SIGNED"),
        (ExitCode.SIGNOFF_REVOKE_ROOM_NOT_FOUND, "SIGNOFF_REVOKE_ROOM_NOT_FOUND"),
        (ExitCode.SIGNOFF_REVOKE_NOT_SIGNED, "SIGNOFF_REVOKE_NOT_SIGNED"),
        (ExitCode.SIGNOFF_AUDIT_MISSING_REASON, "SIGNOFF_AUDIT_MISSING_REASON"),
    ]
    for code, name in exit_codes:
        assert str(code) in readme, f"README 缺少退出码 {code} ({name})"
        assert name in readme, f"README 缺少退出码常量名 {name}"


def test_cli_help_mentions_correct_revoke(runner):
    r = runner.invoke(main, ["--help"])
    top_help = r.stdout + (r.stderr or "")
    assert r.exit_code == 0
    assert "signoff-correct" in top_help
    assert "signoff-revoke" in top_help
    assert "signoff-history" in top_help

    r = runner.invoke(main, ["signoff-correct", "--help"])
    help_text = r.stdout + (r.stderr or "")
    assert r.exit_code == 0
    for kw in ("batch-id", "exam-id", "room-id", "subject", "operator", "reason",
               "signoff-person", "signoff-time", "received-count", "damage-note", "remark"):
        assert kw in help_text, f"signoff-correct --help 缺少选项: {kw}"

    r = runner.invoke(main, ["signoff-revoke", "--help"])
    help_text = r.stdout + (r.stderr or "")
    assert r.exit_code == 0
    for kw in ("batch-id", "exam-id", "room-id", "subject", "operator", "reason"):
        assert kw in help_text, f"signoff-revoke --help 缺少选项: {kw}"

    r = runner.invoke(main, ["signoff-history", "--help"])
    help_text = r.stdout + (r.stderr or "")
    assert r.exit_code == 0
    for kw in ("batch-id", "exam-id", "room-id", "subject", "format"):
        assert kw in help_text, f"signoff-history --help 缺少选项: {kw}"


# ===========================================================================
# 异常处置单 (incident-*) 测试
# ===========================================================================

# ---------------------------------------------------------------------------
# 35. incident-create: 正常登记处置单
# ---------------------------------------------------------------------------

def _create_sample_incident(runner, env, bid, *, room_id="A101", subject="math",
                            incident_type="package_damaged", description="测试说明",
                            operator="张老师", attachments=None):
    """辅助函数：创建一个异常处置单，返回 ticket_id。"""
    args = [
        "--storage-dir", str(env["storage_dir"]),
        "incident-create",
        "--batch-id", bid,
        "--exam-id", "20260608",
        "--room-id", room_id,
        "--subject", subject,
        "--type", incident_type,
        "--description", description,
        "--operator", operator,
    ]
    if attachments:
        for a in attachments:
            args.extend(["--attachment", str(a)])
    r = runner.invoke(main, args, catch_exceptions=False)
    assert r.exit_code == ExitCode.SUCCESS, f"incident-create failed, stdout={r.stdout}\nstderr={r.stderr}"
    assert "处置单创建成功" in r.stdout
    import re
    m = re.search(r"incident-\d{8}-\d{6}-[a-f0-9]{6}", r.stdout)
    assert m, f"未在输出中找到处置单 ID, stdout={r.stdout}"
    return m.group(0)


def test_incident_create_normal(runner, fresh_env):
    env = fresh_env
    bid = _dispatch_successful_batch(runner, env)

    att = env["tmp_path"] / "photo1.jpg"
    att.write_bytes(b"fake image data")

    ticket_id = _create_sample_incident(runner, env, bid, attachments=[att])

    batch = Storage(env["storage_dir"]).get_batch(bid)
    assert len(batch.list_incident_ids()) == 1
    ticket = batch.load_incident(ticket_id)
    assert ticket is not None
    assert ticket.ticket_id == ticket_id
    assert ticket.batch_id == bid
    assert ticket.room_id == "A101"
    assert ticket.subject == "math"
    assert ticket.incident_type.value == "package_damaged"
    assert ticket.description == "测试说明"
    assert ticket.operator == "张老师"
    assert ticket.status.value == "open"
    assert len(ticket.attachment_paths) == 1
    assert len(ticket.handling_records) == 0

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "incident-list", "--batch-id", bid,
            "--format", "json",
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS
    data = json.loads(r.stdout)
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["ticket_id"] == ticket_id
    assert data[0]["status"] == "open"

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "incident-list", "--batch-id", bid,
            "--ticket-id", ticket_id, "--format", "json",
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS
    detail = json.loads(r.stdout)
    assert detail["ticket_id"] == ticket_id
    assert "handling_records" in detail


# ---------------------------------------------------------------------------
# 36. incident-create: 重复创建冲突检测
# ---------------------------------------------------------------------------

def test_incident_create_conflict(runner, fresh_env):
    env = fresh_env
    bid = _dispatch_successful_batch(runner, env)

    _create_sample_incident(runner, env, bid)

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "incident-create",
            "--batch-id", bid,
            "--exam-id", "20260608",
            "--room-id", "A101",
            "--subject", "math",
            "--type", "wrong_package",
            "--description", "另一个问题",
            "--operator", "李老师",
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.INCIDENT_CONFLICT, f"got {r.exit_code}, stdout={r.stdout}"
    output = r.stdout + (r.stderr or "")
    assert "冲突" in output or "未关闭" in output

    batch = Storage(env["storage_dir"]).get_batch(bid)
    assert len(batch.list_incident_ids()) == 1


# ---------------------------------------------------------------------------
# 37. incident-handle: 追加处理记录
# ---------------------------------------------------------------------------

def test_incident_handle_and_close(runner, fresh_env):
    env = fresh_env
    bid = _dispatch_successful_batch(runner, env)
    ticket_id = _create_sample_incident(runner, env, bid)

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "incident-handle",
            "--batch-id", bid,
            "--ticket-id", ticket_id,
            "--operator", "李主任",
            "--action", "联系印刷厂重印",
            "--note", "预计30分钟送达",
            "--to-status", "processing",
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS, f"stdout={r.stdout}\nstderr={r.stderr}"
    output = r.stdout + (r.stderr or "")
    assert "处理记录已追加" in output

    batch = Storage(env["storage_dir"]).get_batch(bid)
    ticket = batch.load_incident(ticket_id)
    assert ticket.status.value == "processing"
    assert len(ticket.handling_records) == 1
    assert ticket.handling_records[0].operator == "李主任"
    assert ticket.handling_records[0].action == "联系印刷厂重印"

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "incident-handle",
            "--batch-id", bid,
            "--ticket-id", ticket_id,
            "--operator", "李主任",
            "--action", "重印件已送达并更换",
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS
    batch = Storage(env["storage_dir"]).get_batch(bid)
    ticket = batch.load_incident(ticket_id)
    assert ticket.status.value == "processing"
    assert len(ticket.handling_records) == 2

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "incident-close",
            "--batch-id", bid,
            "--ticket-id", ticket_id,
            "--operator", "主考",
            "--reason", "问题已解决",
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert "处置单已关闭" in r.stdout

    batch = Storage(env["storage_dir"]).get_batch(bid)
    ticket = batch.load_incident(ticket_id)
    assert ticket.status.value == "closed"
    assert ticket.closed_by == "主考"
    assert ticket.close_reason == "问题已解决"
    assert ticket.closed_at is not None


# ---------------------------------------------------------------------------
# 38. 关闭后再登记（同一考场允许创建新工单）
# ---------------------------------------------------------------------------

def test_incident_create_after_close(runner, fresh_env):
    env = fresh_env
    bid = _dispatch_successful_batch(runner, env)
    ticket_id1 = _create_sample_incident(runner, env, bid)

    runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "incident-close",
            "--batch-id", bid,
            "--ticket-id", ticket_id1,
            "--operator", "主考",
            "--reason", "问题已解决",
        ],
        catch_exceptions=False,
    )

    ticket_id2 = _create_sample_incident(runner, env, bid)
    assert ticket_id1 != ticket_id2

    batch = Storage(env["storage_dir"]).get_batch(bid)
    assert len(batch.list_incident_ids()) == 2


# ---------------------------------------------------------------------------
# 39. incident-handle/close: 已关闭的工单不能操作
# ---------------------------------------------------------------------------

def test_incident_closed_immutable(runner, fresh_env):
    env = fresh_env
    bid = _dispatch_successful_batch(runner, env)
    ticket_id = _create_sample_incident(runner, env, bid)

    runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "incident-close",
            "--batch-id", bid,
            "--ticket-id", ticket_id,
            "--operator", "主考",
            "--reason", "已解决",
        ],
        catch_exceptions=False,
    )

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "incident-handle",
            "--batch-id", bid,
            "--ticket-id", ticket_id,
            "--operator", "李主任",
            "--action", "尝试追加",
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.INCIDENT_ALREADY_CLOSED, f"got {r.exit_code}, stdout={r.stdout}"

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "incident-close",
            "--batch-id", bid,
            "--ticket-id", ticket_id,
            "--operator", "主考",
            "--reason", "再次关闭",
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.INCIDENT_ALREADY_CLOSED


# ---------------------------------------------------------------------------
# 40. incident-create: 附件路径无效时拒绝创建
# ---------------------------------------------------------------------------

def test_incident_create_invalid_attachment(runner, fresh_env):
    env = fresh_env
    bid = _dispatch_successful_batch(runner, env)

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "incident-create",
            "--batch-id", bid,
            "--exam-id", "20260608",
            "--room-id", "A101",
            "--subject", "math",
            "--type", "package_damaged",
            "--description", "有附件路径无效",
            "--operator", "张老师",
            "--attachment", "/definitely/does/not/exist.jpg",
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.INCIDENT_INVALID_FIELD, f"got {r.exit_code}, stdout={r.stdout}"
    output = r.stdout + (r.stderr or "")
    assert "附件" in output or "attachment" in output.lower()

    batch = Storage(env["storage_dir"]).get_batch(bid)
    assert len(batch.list_incident_ids()) == 0
    incident_dir = batch.batch_dir / "incidents"
    assert not incident_dir.exists() or not any(incident_dir.iterdir())


# ---------------------------------------------------------------------------
# 41. incident-create: 必填字段为空时拒绝创建
# ---------------------------------------------------------------------------

def test_incident_create_empty_fields(runner, fresh_env):
    env = fresh_env
    bid = _dispatch_successful_batch(runner, env)

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "incident-create",
            "--batch-id", bid,
            "--exam-id", "20260608",
            "--room-id", "A101",
            "--subject", "math",
            "--type", "package_damaged",
            "--description", "",
            "--operator", "",
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.INCIDENT_INVALID_FIELD

    batch = Storage(env["storage_dir"]).get_batch(bid)
    assert len(batch.list_incident_ids()) == 0


# ---------------------------------------------------------------------------
# 42. 跨重启 query/export 处置单数据一致性
# ---------------------------------------------------------------------------

def test_incident_cross_restart_consistency(runner, fresh_env):
    env = fresh_env
    bid = _dispatch_successful_batch(runner, env)

    t1 = _create_sample_incident(runner, env, bid, room_id="A101", subject="math")
    t2 = _create_sample_incident(runner, env, bid, room_id="B201", subject="english")

    runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "incident-handle",
            "--batch-id", bid,
            "--ticket-id", t1,
            "--operator", "李主任",
            "--action", "处理中",
            "--to-status", "processing",
        ],
        catch_exceptions=False,
    )
    runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "incident-close",
            "--batch-id", bid,
            "--ticket-id", t2,
            "--operator", "主考",
            "--reason", "已解决",
        ],
        catch_exceptions=False,
    )

    out_json1 = env["tmp_path"] / "incident_before.json"
    out_csv1 = env["tmp_path"] / "incident_batch_before.csv"
    for fmt, path in (("json", out_json1), ("csv", out_csv1)):
        r = runner.invoke(
            main,
            [
                "--storage-dir", str(env["storage_dir"]),
                "export", "--format", fmt, "--output", str(path),
            ],
            catch_exceptions=False,
        )
        assert r.exit_code == ExitCode.SUCCESS

    out_zip1 = env["tmp_path"] / "audit_incident_before.zip"
    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "audit-pack", "--batch-id", bid, "--output", str(out_zip1),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS

    Storage2 = Storage
    storage2 = Storage2(env["storage_dir"])
    batch2 = storage2.get_batch(bid)
    assert len(batch2.list_incident_ids()) == 2
    counts = batch2.count_incidents()
    assert counts["open"] + counts["processing"] == 1
    assert counts["closed"] == 1

    out_json2 = env["tmp_path"] / "incident_after.json"
    out_csv2 = env["tmp_path"] / "incident_batch_after.csv"
    for fmt, path in (("json", out_json2), ("csv", out_csv2)):
        r = runner.invoke(
            main,
            [
                "--storage-dir", str(env["storage_dir"]),
                "export", "--format", fmt, "--output", str(path),
            ],
            catch_exceptions=False,
        )
        assert r.exit_code == ExitCode.SUCCESS

    data1 = json.loads(out_json1.read_text(encoding="utf-8"))
    data2 = json.loads(out_json2.read_text(encoding="utf-8"))
    b1 = data1["batches"][0]
    b2 = data2["batches"][0]
    assert b1["incidents"]["total"] == b2["incidents"]["total"] == 2
    assert b1["incidents"]["open"] + b1["incidents"]["processing"] == 1
    assert b2["incidents"]["open"] + b2["incidents"]["processing"] == 1
    assert b1["incidents"]["closed"] == b2["incidents"]["closed"] == 1
    assert len(b1["incidents"]["items"]) == len(b2["incidents"]["items"]) == 2

    import csv as csv_mod
    with out_csv1.open("r", encoding="utf-8-sig") as f:
        rows1 = list(csv_mod.DictReader(f))
    with out_csv2.open("r", encoding="utf-8-sig") as f:
        rows2 = list(csv_mod.DictReader(f))
    assert rows1[0]["incident_count"] == rows2[0]["incident_count"] == "2"
    assert rows1[0]["incident_closed_count"] == rows2[0]["incident_closed_count"] == "1"

    out_zip2 = env["tmp_path"] / "audit_incident_after.zip"
    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "audit-pack", "--batch-id", bid, "--output", str(out_zip2),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS
    with zipfile.ZipFile(out_zip2, "r") as zf:
        names = set(zf.namelist())
        assert "incidents_index.json" in names
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert manifest["counts"]["incident_total"] == 2
        assert manifest["counts"]["incident_closed"] == 1
        readme = zf.read("README.txt").decode("utf-8")
        assert "异常处置单" in readme


# ---------------------------------------------------------------------------
# 43. incident-create: I/O 错误时不留下半成品
# ---------------------------------------------------------------------------

def test_incident_create_io_error_no_half_file(runner, fresh_env, monkeypatch):
    env = fresh_env
    bid = _dispatch_successful_batch(runner, env)

    from exam_paper_dispatcher import storage as st_mod

    real_save = st_mod.BatchState.save_incident

    def bad_save(self, *args, **kwargs):
        raise OSError("simulated disk full")

    monkeypatch.setattr(st_mod.BatchState, "save_incident", bad_save)

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "incident-create",
            "--batch-id", bid,
            "--exam-id", "20260608",
            "--room-id", "A101",
            "--subject", "math",
            "--type", "package_damaged",
            "--description", "测试 IO 错误",
            "--operator", "张老师",
        ],
    )
    assert r.exit_code != ExitCode.SUCCESS, f"stdout={r.stdout}"

    batch = Storage(env["storage_dir"]).get_batch(bid)
    assert len(batch.list_incident_ids()) == 0
    incident_dir = batch.batch_dir / "incidents"
    assert not incident_dir.exists() or not any(incident_dir.iterdir())


# ---------------------------------------------------------------------------
# 44. query 和 export 包含处置单摘要
# ---------------------------------------------------------------------------

def test_incident_query_and_export_summary(runner, fresh_env):
    env = fresh_env
    bid = _dispatch_successful_batch(runner, env)

    t1 = _create_sample_incident(runner, env, bid)
    t2 = _create_sample_incident(runner, env, bid, room_id="B201", subject="english")

    runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "incident-close",
            "--batch-id", bid,
            "--ticket-id", t2,
            "--operator", "主考",
            "--reason", "已解决",
        ],
        catch_exceptions=False,
    )

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "query",
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS
    list_output = r.stdout
    assert "1/1" in list_output

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "query", "--batch-id", bid,
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS
    detail = json.loads(r.stdout)
    assert "incidents" in detail
    assert detail["incidents"]["total"] == 2
    assert detail["incidents"]["closed"] == 1


# ---------------------------------------------------------------------------
# 45. README & CLI help 提及异常处置单命令和退出码
# ---------------------------------------------------------------------------

def test_readme_mentions_incident():
    readme = README_PATH.read_text(encoding="utf-8")
    for keyword in (
        "incident-create", "incident-list", "incident-handle", "incident-close",
        "异常处置单", "处置单", "工单",
    ):
        assert keyword in readme, f"README 缺少关键字: {keyword}"


def test_readme_incident_exit_codes_match_models():
    readme = README_PATH.read_text(encoding="utf-8")
    exit_codes = [
        (ExitCode.INCIDENT_CONFLICT, "INCIDENT_CONFLICT"),
        (ExitCode.INCIDENT_NOT_FOUND, "INCIDENT_NOT_FOUND"),
        (ExitCode.INCIDENT_ALREADY_CLOSED, "INCIDENT_ALREADY_CLOSED"),
        (ExitCode.INCIDENT_INVALID_FIELD, "INCIDENT_INVALID_FIELD"),
        (ExitCode.INCIDENT_BATCH_NOT_DISPATCHED, "INCIDENT_BATCH_NOT_DISPATCHED"),
    ]
    for code, name in exit_codes:
        assert str(code) in readme, f"README 缺少退出码 {code} ({name})"
        assert name in readme, f"README 缺少退出码常量名 {name}"


def test_cli_help_mentions_incident(runner):
    r = runner.invoke(main, ["--help"])
    top_help = r.stdout + (r.stderr or "")
    assert r.exit_code == 0
    assert "incident-create" in top_help
    assert "incident-list" in top_help
    assert "incident-handle" in top_help
    assert "incident-close" in top_help

    for cmd in ("incident-create", "incident-list", "incident-handle", "incident-close"):
        r = runner.invoke(main, [cmd, "--help"])
        help_text = r.stdout + (r.stderr or "")
        assert r.exit_code == 0, f"{cmd} --help exit code = {r.exit_code}"

    r = runner.invoke(main, ["incident-create", "--help"])
    help_text = r.stdout + (r.stderr or "")
    for kw in ("batch-id", "exam-id", "room-id", "subject", "type",
               "description", "operator", "attachment"):
        assert kw in help_text, f"incident-create --help 缺少选项: {kw}"
    assert "已发放" in help_text or "completed" in help_text, \
        "incident-create --help 未说明只有已发放批次才能建单"
    assert "审计日志" in help_text, \
        "incident-create --help 未说明会写入审计日志"

    r = runner.invoke(main, ["incident-handle", "--help"])
    help_text = r.stdout + (r.stderr or "")
    for kw in ("batch-id", "ticket-id", "operator", "action", "note", "to-status"):
        assert kw in help_text, f"incident-handle --help 缺少选项: {kw}"
    assert "审计日志" in help_text, \
        "incident-handle --help 未说明会写入审计日志"

    r = runner.invoke(main, ["incident-close", "--help"])
    help_text = r.stdout + (r.stderr or "")
    for kw in ("batch-id", "ticket-id", "operator", "reason"):
        assert kw in help_text, f"incident-close --help 缺少选项: {kw}"
    assert "审计日志" in help_text, \
        "incident-close --help 未说明会写入审计日志"


# ---------------------------------------------------------------------------
# 44. incident-create: 未发放批次被拒绝
# ---------------------------------------------------------------------------

def test_incident_rejected_before_dispatch(runner, fresh_env):
    env = fresh_env
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
    assert r.exit_code == ExitCode.SUCCESS
    bid = Storage(env["storage_dir"]).list_batches()[0].batch_id

    batch = Storage(env["storage_dir"]).get_batch(bid)
    assert batch.status.value != "completed"
    assert batch.status.value != "rolled_back"

    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "incident-create",
            "--batch-id", bid,
            "--exam-id", "20260608",
            "--room-id", "A101",
            "--subject", "math",
            "--type", "package_damaged",
            "--description", "测试未发放拒绝",
            "--operator", "张老师",
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.INCIDENT_BATCH_NOT_DISPATCHED, \
        f"got {r.exit_code}, stdout={r.stdout}"
    combined = r.stdout + (r.stderr or "")
    assert ("完成发放" in combined) or ("completed" in combined) or ("已发放" in combined)

    batch2 = Storage(env["storage_dir"]).get_batch(bid)
    assert len(batch2.list_incident_ids()) == 0
    assert len(batch2.load_incident_audit_log()) == 0


# ---------------------------------------------------------------------------
# 45. incident: 审计日志跨重启一致性 + query/export/audit-pack 包含审计日志
# ---------------------------------------------------------------------------

def test_incident_audit_log_consistency(runner, fresh_env):
    env = fresh_env
    bid = _dispatch_successful_batch(runner, env)

    t1 = _create_sample_incident(runner, env, bid, room_id="A101", subject="math")
    runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "incident-handle",
            "--batch-id", bid,
            "--ticket-id", t1,
            "--operator", "李主任",
            "--action", "联系考场确认情况",
            "--note", "考场反馈第3份有轻微压痕",
            "--to-status", "processing",
        ],
        catch_exceptions=False,
    )
    runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "incident-close",
            "--batch-id", bid,
            "--ticket-id", t1,
            "--operator", "主考",
            "--reason", "考生正常使用，无需更换",
        ],
        catch_exceptions=False,
    )

    batch = Storage(env["storage_dir"]).get_batch(bid)
    audit_log_before = batch.load_incident_audit_log()
    assert len(audit_log_before) == 3
    actions = [a.action.value for a in audit_log_before]
    assert "create" in actions
    assert "handle" in actions
    assert "close" in actions
    for entry in audit_log_before:
        assert entry.ticket_id == t1
        assert entry.batch_id == bid
        assert entry.timestamp is not None
        assert entry.operator is not None

    out_json = env["tmp_path"] / "incident_audit_before.json"
    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "export", "--format", "json", "--output", str(out_json),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS
    data = json.loads(out_json.read_text(encoding="utf-8"))
    b = data["batches"][0]
    assert "incident_audit_log" in b
    assert len(b["incident_audit_log"]) == 3
    assert b["incidents"]["audit_count"] == 3

    out_zip = env["tmp_path"] / "audit_incident_audit.zip"
    r = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "audit-pack", "--batch-id", bid, "--output", str(out_zip),
        ],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS
    with zipfile.ZipFile(out_zip, "r") as zf:
        names = set(zf.namelist())
        assert "incident_audit_log.jsonl" in names
        audit_raw = zf.read("incident_audit_log.jsonl").decode("utf-8")
        audit_lines = [l for l in audit_raw.split("\n") if l.strip()]
        assert len(audit_lines) == 3
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert manifest["counts"]["incident_audit_total"] == 3
        readme = zf.read("README.txt").decode("utf-8")
        assert "审计日志总数" in readme

    Storage2 = Storage
    storage2 = Storage2(env["storage_dir"])
    batch2 = storage2.get_batch(bid)
    audit_log_after = batch2.load_incident_audit_log()
    assert len(audit_log_after) == 3
    for before, after in zip(audit_log_before, audit_log_after):
        assert before.audit_id == after.audit_id
        assert before.action == after.action
        assert before.operator == after.operator
        assert before.ticket_id == after.ticket_id
        assert before.batch_id == after.batch_id


# ---------------------------------------------------------------------------
# 46. incident: 追加处理记录后审计日志正确
# ---------------------------------------------------------------------------

def test_incident_handle_appends_audit_log(runner, fresh_env):
    env = fresh_env
    bid = _dispatch_successful_batch(runner, env)
    ticket_id = _create_sample_incident(runner, env, bid, room_id="A101", subject="math")

    batch = Storage(env["storage_dir"]).get_batch(bid)
    assert len(batch.load_incident_audit_log()) == 1

    runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "incident-handle",
            "--batch-id", bid,
            "--ticket-id", ticket_id,
            "--operator", "巡考A",
            "--action", "第一次处理",
            "--note", "初步核实",
        ],
        catch_exceptions=False,
    )
    assert len(batch.load_incident_audit_log()) == 2

    runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "incident-handle",
            "--batch-id", bid,
            "--ticket-id", ticket_id,
            "--operator", "巡考B",
            "--action", "第二次处理",
            "--note", "确认解决方案",
            "--to-status", "processing",
        ],
        catch_exceptions=False,
    )
    assert len(batch.load_incident_audit_log()) == 3

    audit_log = batch.load_incident_audit_log()
    handle_entries = [a for a in audit_log if a.action.value == "handle"]
    assert len(handle_entries) == 2
    assert handle_entries[0].operator == "巡考A"
    assert handle_entries[1].operator == "巡考B"
    assert handle_entries[1].status_after == "processing"

    ticket = batch.load_incident(ticket_id)
    assert len(ticket.handling_records) == 2


# ---------------------------------------------------------------------------
# 47. incident: 创建→处理→关闭 完整流程回归 + 四个出口一致性
# ---------------------------------------------------------------------------

def test_incident_full_lifecycle_and_four_exports(runner, fresh_env):
    env = fresh_env
    bid = _dispatch_successful_batch(runner, env)

    # ---- 1. 创建处置单（未处理 open）----
    ticket_id = _create_sample_incident(
        runner, env, bid,
        room_id="A101", subject="math",
        incident_type="package_damaged",
        description="试卷包外封破损，需确认内容完整性",
        operator="监考员甲",
    )

    batch = Storage(env["storage_dir"]).get_batch(bid)
    ticket_after_create = batch.load_incident(ticket_id)
    assert ticket_after_create.status.value == "open"
    assert ticket_after_create.operator == "监考员甲"

    # ---- 2. 处理（切换到 processing）----
    r_handle = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "incident-handle",
            "--batch-id", bid,
            "--ticket-id", ticket_id,
            "--operator", "巡考员乙",
            "--action", "现场核验试卷内容",
            "--note", "试卷内容完整无缺页，仅外封轻微破损",
            "--to-status", "processing",
        ],
        catch_exceptions=False,
    )
    assert r_handle.exit_code == ExitCode.SUCCESS, f"stdout={r_handle.stdout}"
    assert "处理记录已追加" in r_handle.stdout

    # ---- 3. 关闭（从 processing → closed）----
    close_operator = "主考丙"
    close_reason = "确认不影响使用，考生正常作答"
    r_close = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "incident-close",
            "--batch-id", bid,
            "--ticket-id", ticket_id,
            "--operator", close_operator,
            "--reason", close_reason,
        ],
        catch_exceptions=False,
    )
    assert r_close.exit_code == ExitCode.SUCCESS, f"stdout={r_close.stdout}"
    assert "已关闭" in r_close.stdout

    # ---- 加载最终状态 ----
    batch = Storage(env["storage_dir"]).get_batch(bid)
    ticket_final = batch.load_incident(ticket_id)
    assert ticket_final.status.value == "closed"
    assert ticket_final.closed_by == close_operator
    assert ticket_final.close_reason == close_reason
    assert ticket_final.closed_at is not None
    assert len(ticket_final.handling_records) == 1

    audit_log = batch.load_incident_audit_log()
    assert len(audit_log) == 3  # create + handle + close

    # ---- 出口 A: 查询详情（incident-list --ticket-id --format json）----
    r_detail = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "incident-list",
            "--batch-id", bid,
            "--ticket-id", ticket_id,
            "--format", "json",
        ],
        catch_exceptions=False,
    )
    assert r_detail.exit_code == ExitCode.SUCCESS
    detail = json.loads(r_detail.stdout)
    assert detail["ticket_id"] == ticket_id
    assert detail["status"] == "closed"
    assert detail["closed_by"] == close_operator
    assert detail["close_reason"] == close_reason
    assert detail["handling_count"] == 1
    assert len(detail["handling_records"]) == 1
    assert detail["handling_records"][0]["operator"] == "巡考员乙"
    assert detail["handling_records"][0]["action"] == "现场核验试卷内容"
    assert detail["operator"] == "监考员甲"  # 创建人

    # ---- 出口 B: 列表汇总（query）----
    r_query_list = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "query",
        ],
        catch_exceptions=False,
    )
    assert r_query_list.exit_code == ExitCode.SUCCESS
    # 处置单摘要显示：未处理/处理中 + 已关闭 → 0/1
    assert "0/1" in r_query_list.stdout

    r_query_detail = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "query", "--batch-id", bid,
        ],
        catch_exceptions=False,
    )
    assert r_query_detail.exit_code == ExitCode.SUCCESS
    query_data = json.loads(r_query_detail.stdout)
    assert "incidents" in query_data
    inc_summary = query_data["incidents"]
    assert inc_summary["total"] == 1
    assert inc_summary["count"] == 1
    assert inc_summary["open_count"] == 0
    assert inc_summary["open"] == 0
    assert inc_summary["processing_count"] == 0
    assert inc_summary["processing"] == 0
    assert inc_summary["closed_count"] == 1
    assert inc_summary["closed"] == 1
    assert inc_summary["audit_count"] == 3
    assert len(inc_summary["tickets"]) == 1
    assert inc_summary["tickets"][0]["ticket_id"] == ticket_id
    assert inc_summary["tickets"][0]["status"] == "closed"
    assert inc_summary["tickets"][0]["handling_count"] == 1
    assert "index" in inc_summary

    # 审计日志嵌入 query 结果
    assert "incident_audit_log" in query_data
    assert len(query_data["incident_audit_log"]) == 3

    # ---- 出口 C: JSON 导出 ----
    out_json = env["tmp_path"] / "incident_export.json"
    r_json = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "export", "--format", "json",
            "--output", str(out_json),
        ],
        catch_exceptions=False,
    )
    assert r_json.exit_code == ExitCode.SUCCESS
    json_data = json.loads(out_json.read_text(encoding="utf-8"))
    b_export = json_data["batches"][0]
    assert b_export["incidents"]["total"] == 1
    assert b_export["incidents"]["closed"] == 1
    assert b_export["incidents"]["audit_count"] == 3
    assert len(b_export["incident_audit_log"]) == 3

    # ---- 出口 C: CSV 导出（batches）----
    out_csv = env["tmp_path"] / "incident_batches.csv"
    r_csv = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "export", "--format", "csv",
            "--output", str(out_csv),
        ],
        catch_exceptions=False,
    )
    assert r_csv.exit_code == ExitCode.SUCCESS
    import csv as csv_mod
    with out_csv.open("r", encoding="utf-8-sig") as f:
        csv_rows = list(csv_mod.DictReader(f))
    assert len(csv_rows) == 1
    csv_row = csv_rows[0]
    assert csv_row["batch_id"] == bid
    for key in (
        "incident_count", "incident_open_count", "incident_processing_count",
        "incident_closed_count", "incident_audit_count",
    ):
        assert key in csv_row, f"CSV 缺少列: {key}"
    assert csv_row["incident_count"] == "1"
    assert csv_row["incident_open_count"] == "0"
    assert csv_row["incident_processing_count"] == "0"
    assert csv_row["incident_closed_count"] == "1"
    assert csv_row["incident_audit_count"] == "3"

    # ---- 出口 D: 审计包 ----
    out_zip = env["tmp_path"] / "audit_incident_lifecycle.zip"
    r_audit = runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "audit-pack", "--batch-id", bid,
            "--output", str(out_zip),
        ],
        catch_exceptions=False,
    )
    assert r_audit.exit_code == ExitCode.SUCCESS

    with zipfile.ZipFile(out_zip, "r") as zf:
        names = set(zf.namelist())

        # 必须包含的处置单相关文件
        assert "incident_summary.json" in names
        assert "incidents_index.json" in names
        assert "incident_audit_log.jsonl" in names
        ticket_file = f"incidents/{ticket_id}.json"
        assert ticket_file in names

        # 处置单汇总文件
        summary = json.loads(zf.read("incident_summary.json").decode("utf-8"))
        assert summary["total"] == 1
        assert summary["closed"] == 1
        assert summary["audit_count"] == 3

        # 索引文件
        index = json.loads(zf.read("incidents_index.json").decode("utf-8"))
        assert ticket_id in index
        assert index[ticket_id]["status"] == "closed"
        assert index[ticket_id]["handling_count"] == 1

        # 审计日志 jsonl（顺序和字段）
        audit_raw = zf.read("incident_audit_log.jsonl").decode("utf-8")
        audit_lines = [l for l in audit_raw.split("\n") if l.strip()]
        assert len(audit_lines) == 3
        audit_entries = [json.loads(l) for l in audit_lines]
        actions = [e["action"] for e in audit_entries]
        assert actions == ["create", "handle", "close"], f"审计日志顺序错误: {actions}"

        # CREATE 审计字段
        create_entry = audit_entries[0]
        assert create_entry["action"] == "create"
        assert create_entry["ticket_id"] == ticket_id
        assert create_entry["operator"] == "监考员甲"
        assert create_entry["status_before"] is None
        assert create_entry["status_after"] == "open"
        assert create_entry["exam_id"] == "20260608"
        assert create_entry["room_id"] == "A101"
        assert create_entry["subject"] == "math"
        assert create_entry["audit_id"].startswith("incident-audit-")
        assert "timestamp" in create_entry

        # HANDLE 审计字段
        handle_entry = audit_entries[1]
        assert handle_entry["action"] == "handle"
        assert handle_entry["ticket_id"] == ticket_id
        assert handle_entry["operator"] == "巡考员乙"
        assert handle_entry["status_before"] == "open"
        assert handle_entry["status_after"] == "processing"
        assert "现场核验" in handle_entry["detail"]

        # CLOSE 审计字段（关键：旧状态应为 processing 而非 open）
        close_entry = audit_entries[2]
        assert close_entry["action"] == "close"
        assert close_entry["ticket_id"] == ticket_id
        assert close_entry["operator"] == close_operator
        assert close_entry["status_before"] == "processing"  # 修复后应为 processing
        assert close_entry["status_after"] == "closed"
        assert close_reason in close_entry["detail"]

        # manifest 计数
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert manifest["counts"]["incident_total"] == 1
        assert manifest["counts"]["incident_open"] == 0
        assert manifest["counts"]["incident_processing"] == 0
        assert manifest["counts"]["incident_closed"] == 1
        assert manifest["counts"]["incident_audit_total"] == 3

        # README 包含处置单摘要
        readme = zf.read("README.txt").decode("utf-8")
        assert "异常处置单总数" in readme
        assert "已关闭" in readme

    # ---- 关闭后同考场可再次建新单 ----
    ticket_id2 = _create_sample_incident(
        runner, env, bid,
        room_id="A101", subject="math",
        incident_type="other",
        description="同考场二次登记，用于验证关闭后可再次建单",
        operator="监考员丁",
    )
    assert ticket_id2 != ticket_id

    batch_after = Storage(env["storage_dir"]).get_batch(bid)
    counts_after = batch_after.count_incidents()
    assert counts_after["total"] == 2
    assert counts_after["open"] == 1
    assert counts_after["closed"] == 1
    assert len(batch_after.load_incident_audit_log()) == 4  # 原3条 + 新create


# ---------------------------------------------------------------------------
# 48. incident: 跨重启一致性（汇总/索引/审计日志/归档清单）
# ---------------------------------------------------------------------------

def test_incident_across_restart_fields_and_order(runner, fresh_env):
    env = fresh_env
    bid = _dispatch_successful_batch(runner, env)

    # 建两张单：一张处理中，一张已关闭
    t_open = _create_sample_incident(
        runner, env, bid, room_id="A101", subject="math",
        description="待处理单", operator="创建人A",
    )
    t_closed = _create_sample_incident(
        runner, env, bid, room_id="B201", subject="english",
        description="已关闭单", operator="创建人B",
    )

    runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "incident-handle",
            "--batch-id", bid, "--ticket-id", t_open,
            "--operator", "处理人X", "--action", "初步跟进",
            "--to-status", "processing",
        ],
        catch_exceptions=False,
    )
    runner.invoke(
        main,
        [
            "--storage-dir", str(env["storage_dir"]),
            "incident-close",
            "--batch-id", bid, "--ticket-id", t_closed,
            "--operator", "关闭人Y", "--reason", "已处置完毕",
        ],
        catch_exceptions=False,
    )

    # ---- 重启前：导出并记录 ----
    out_json1 = env["tmp_path"] / "incident_r1.json"
    out_csv1 = env["tmp_path"] / "incident_r1.csv"
    out_zip1 = env["tmp_path"] / "audit_r1.zip"
    for fmt, path in (("json", out_json1), ("csv", out_csv1)):
        r = runner.invoke(
            main,
            ["--storage-dir", str(env["storage_dir"]),
             "export", "--format", fmt, "--output", str(path)],
            catch_exceptions=False,
        )
        assert r.exit_code == ExitCode.SUCCESS
    r = runner.invoke(
        main,
        ["--storage-dir", str(env["storage_dir"]),
         "audit-pack", "--batch-id", bid, "--output", str(out_zip1)],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS

    r_list1 = runner.invoke(
        main,
        ["--storage-dir", str(env["storage_dir"]),
         "incident-list", "--batch-id", bid, "--format", "json"],
        catch_exceptions=False,
    )
    assert r_list1.exit_code == ExitCode.SUCCESS
    list_data1 = json.loads(r_list1.stdout)

    r_query1 = runner.invoke(
        main,
        ["--storage-dir", str(env["storage_dir"]),
         "query", "--batch-id", bid],
        catch_exceptions=False,
    )
    assert r_query1.exit_code == ExitCode.SUCCESS
    query_data1 = json.loads(r_query1.stdout)

    # ---- 模拟"重启"：重建 Storage 对象 ----
    Storage2 = Storage
    storage_after = Storage2(env["storage_dir"])
    batch_after = storage_after.get_batch(bid)
    assert batch_after is not None

    # 汇总计数不丢字段
    counts_after = batch_after.count_incidents()
    assert "total" in counts_after
    assert "open" in counts_after
    assert "processing" in counts_after
    assert "closed" in counts_after
    assert counts_after["total"] == 2
    assert counts_after["open"] + counts_after["processing"] == 1
    assert counts_after["closed"] == 1

    # 索引不丢字段
    idx_after = batch_after.load_incident_index()
    assert t_open in idx_after
    assert t_closed in idx_after
    for tid in (t_open, t_closed):
        for key in ("ticket_id", "created_at", "status", "exam_id",
                    "room_id", "subject", "incident_type", "operator",
                    "handling_count"):
            assert key in idx_after[tid], f"索引缺少字段: {tid}.{key}"

    # 审计日志：条数、顺序、字段
    audit_after = batch_after.load_incident_audit_log()
    assert len(audit_after) == 4  # create×2 + handle + close
    action_seq = [a.action.value for a in audit_after]
    assert action_seq == ["create", "create", "handle", "close"], \
        f"审计日志顺序错乱: {action_seq}"
    for entry in audit_after:
        for key in ("audit_id", "batch_id", "ticket_id", "exam_id",
                    "room_id", "subject", "action", "operator",
                    "detail", "status_before", "status_after", "timestamp"):
            assert hasattr(entry, key) or key in entry.model_dump(), \
                f"审计日志条目缺少字段: {key}"

    # ---- 重启后：再次导出并比对 ----
    out_json2 = env["tmp_path"] / "incident_r2.json"
    out_csv2 = env["tmp_path"] / "incident_r2.csv"
    out_zip2 = env["tmp_path"] / "audit_r2.zip"
    for fmt, path in (("json", out_json2), ("csv", out_csv2)):
        r = runner.invoke(
            main,
            ["--storage-dir", str(env["storage_dir"]),
             "export", "--format", fmt, "--output", str(path)],
            catch_exceptions=False,
        )
        assert r.exit_code == ExitCode.SUCCESS
    r = runner.invoke(
        main,
        ["--storage-dir", str(env["storage_dir"]),
         "audit-pack", "--batch-id", bid, "--output", str(out_zip2)],
        catch_exceptions=False,
    )
    assert r.exit_code == ExitCode.SUCCESS

    r_list2 = runner.invoke(
        main,
        ["--storage-dir", str(env["storage_dir"]),
         "incident-list", "--batch-id", bid, "--format", "json"],
        catch_exceptions=False,
    )
    assert r_list2.exit_code == ExitCode.SUCCESS
    list_data2 = json.loads(r_list2.stdout)

    r_query2 = runner.invoke(
        main,
        ["--storage-dir", str(env["storage_dir"]),
         "query", "--batch-id", bid],
        catch_exceptions=False,
    )
    assert r_query2.exit_code == ExitCode.SUCCESS
    query_data2 = json.loads(r_query2.stdout)

    # 列表（ticket_id 顺序一致）
    ids1 = [t["ticket_id"] for t in list_data1]
    ids2 = [t["ticket_id"] for t in list_data2]
    assert ids1 == ids2, "重启后处置单列表顺序不一致"
    for t1, t2 in zip(list_data1, list_data2):
        for key in ("ticket_id", "status", "handling_count",
                    "operator", "room_id", "subject"):
            assert t1[key] == t2[key], f"列表字段不一致: {key}"

    # query 汇总字段一致
    for key in ("total", "open", "processing", "closed", "audit_count"):
        assert query_data1["incidents"][key] == query_data2["incidents"][key]

    # query 审计日志顺序一致
    audit_q1 = query_data1["incident_audit_log"]
    audit_q2 = query_data2["incident_audit_log"]
    assert len(audit_q1) == len(audit_q2)
    for a1, a2 in zip(audit_q1, audit_q2):
        for key in ("audit_id", "action", "operator", "ticket_id",
                    "status_before", "status_after"):
            assert a1[key] == a2[key], f"审计日志字段不一致: {key}"

    # JSON 导出内容一致（忽略时间相关的顶层差异）
    data_j1 = json.loads(out_json1.read_text(encoding="utf-8"))
    data_j2 = json.loads(out_json2.read_text(encoding="utf-8"))
    b_j1 = data_j1["batches"][0]
    b_j2 = data_j2["batches"][0]
    for key in ("count", "open", "processing", "closed", "audit_count"):
        assert b_j1["incidents"][key] == b_j2["incidents"][key]
    assert len(b_j1["incident_audit_log"]) == len(b_j2["incident_audit_log"])

    # CSV 导出一致
    import csv as csv_mod
    with out_csv1.open("r", encoding="utf-8-sig") as f:
        rows_csv1 = list(csv_mod.DictReader(f))
    with out_csv2.open("r", encoding="utf-8-sig") as f:
        rows_csv2 = list(csv_mod.DictReader(f))
    assert len(rows_csv1) == len(rows_csv2) == 1
    for key in ("incident_count", "incident_open_count",
                "incident_processing_count", "incident_closed_count",
                "incident_audit_count"):
        assert rows_csv1[0][key] == rows_csv2[0][key]

    # 审计包：归档清单文件列表一致
    with zipfile.ZipFile(out_zip1, "r") as zf1, zipfile.ZipFile(out_zip2, "r") as zf2:
        names1 = sorted(zf1.namelist())
        names2 = sorted(zf2.namelist())
        assert names1 == names2, "重启后审计包归档文件列表不一致"
        for fname in names1:
            if fname == "manifest.json":
                m1 = json.loads(zf1.read(fname).decode("utf-8"))
                m2 = json.loads(zf2.read(fname).decode("utf-8"))
                for k in ("batch_id", "batch_status"):
                    assert m1[k] == m2[k]
                for k, v in m1["counts"].items():
                    if k.startswith("incident_"):
                        assert m2["counts"].get(k) == v, \
                            f"manifest 计数不一致: {k}"
            elif fname == "README.txt":
                continue
            elif fname.endswith(".jsonl"):
                # 审计日志行顺序一致
                lines1 = [l for l in zf1.read(fname).decode("utf-8").split("\n") if l.strip()]
                lines2 = [l for l in zf2.read(fname).decode("utf-8").split("\n") if l.strip()]
                assert len(lines1) == len(lines2)
                for l1, l2 in zip(lines1, lines2):
                    e1 = json.loads(l1)
                    e2 = json.loads(l2)
                    for k in ("audit_id", "action", "operator", "ticket_id",
                              "status_before", "status_after"):
                        assert e1[k] == e2[k]
            elif fname in ("incident_summary.json", "incidents_index.json"):
                d1 = json.loads(zf1.read(fname).decode("utf-8"))
                d2 = json.loads(zf2.read(fname).decode("utf-8"))
                assert d1 == d2, f"重启后 {fname} 内容不一致"
            elif fname.startswith("incidents/") and fname.endswith(".json"):
                t1 = json.loads(zf1.read(fname).decode("utf-8"))
                t2 = json.loads(zf2.read(fname).decode("utf-8"))
                for k in ("ticket_id", "status", "closed_by", "close_reason",
                          "handling_count", "operator"):
                    v1 = t1.get(k, t1.get("handling_records") and len(t1.get("handling_records")) if k == "handling_count" else None)
                    v2 = t2.get(k, t2.get("handling_records") and len(t2.get("handling_records")) if k == "handling_count" else None)
                    if k == "handling_count":
                        assert len(t1["handling_records"]) == len(t2["handling_records"])
                    else:
                        assert v1 == v2, f"处置单文件字段不一致: {fname}.{k}"

