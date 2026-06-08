from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .models import (
    BatchStatus,
    DispatchConfig,
    ExitCode,
    RoomRow,
)
from .storage import Storage
from .precheck import PreCheckError, precheck_and_save
from .dispatcher import DispatchError, run_dispatch_from_precheck
from .rollback import RollbackError, rollback_batch
from .query_export import (
    export_batches_csv,
    export_items_csv,
    export_to_json,
    list_batches,
    query_batch,
)

console = Console()
err_console = Console(stderr=True, style="bold red")


def _load_config(ctx: click.Context, config_path: str) -> DispatchConfig:
    try:
        return DispatchConfig.load(config_path)
    except Exception as e:
        err_console.print(f"[配置错误] {e}")
        ctx.exit(ExitCode.CONFIG_ERROR)


def _load_rows(ctx: click.Context, csv_path: str) -> list[RoomRow]:
    try:
        return RoomRow.from_csv(csv_path)
    except FileNotFoundError as e:
        err_console.print(f"[CSV错误] {e}")
        ctx.exit(ExitCode.INVALID_CSV)
    except ValueError as e:
        err_console.print(f"[CSV错误] {e}")
        ctx.exit(ExitCode.INVALID_CSV)
    except Exception as e:
        err_console.print(f"[CSV错误] 读取失败: {e}")
        ctx.exit(ExitCode.INVALID_CSV)


def _print_precheck_report(report, error: Optional[PreCheckError]):
    title = "预检通过" if report.passed else "预检失败"
    style = "green" if report.passed else "red"
    p = Panel.fit(
        f"批次: [bold]{report.batch_id}[/bold]\n"
        f"总行数: {report.total_rows}  |  有效: {report.valid_rows}\n"
        f"缺失源文件: {len(report.missing_sources)}  |  目标冲突: {len(report.target_conflicts)}\n"
        f"非法科目: {len(report.invalid_subjects)}  |  版本问题: {len(report.invalid_versions)}",
        title=f"[{style}]{title}[/{style}]",
        border_style=style,
    )
    console.print(p)

    if report.missing_sources:
        t = Table(title="缺失源文件", show_lines=True)
        t.add_column("行号", style="dim")
        t.add_column("考场")
        t.add_column("科目")
        t.add_column("源文件")
        t.add_column("解析路径", style="red")
        for m in report.missing_sources:
            t.add_row(
                str(m.get("line_no", "")),
                m.get("room_id", ""),
                m.get("subject", ""),
                m.get("source_file", ""),
                m.get("resolved_path", ""),
            )
        console.print(t)

    if report.target_conflicts:
        t = Table(title="目标文件名冲突", show_lines=True)
        t.add_column("目标名", style="yellow")
        t.add_column("涉及行")
        for c in report.target_conflicts:
            rows_desc = ", ".join(
                f"行{r['line_no']}({r['room_id']}/{r['subject']})"
                for r in c["rows"]
            )
            t.add_row(c["target_name"], rows_desc)
        console.print(t)

    if report.invalid_subjects:
        t = Table(title="非法科目或人数")
        t.add_column("行号")
        t.add_column("科目")
        t.add_column("说明")
        for m in report.invalid_subjects:
            t.add_row(str(m.get("line_no", "")), m.get("subject", ""), m.get("message", str(m.get("allowed", ""))))
        console.print(t)

    if report.invalid_versions:
        t = Table(title="版本问题")
        t.add_column("行号")
        t.add_column("科目")
        t.add_column("说明")
        for m in report.invalid_versions:
            t.add_row(str(m.get("line_no", "")), m.get("subject", ""), m.get("message", ""))
        console.print(t)

    if not report.passed and error:
        err_console.print(f"\n退出码: {error.exit_code}")


@click.group(help="离线考试试卷包校验与发放 CLI")
@click.option("--storage-dir", default=".exam_dispatch_state", show_default=True,
              help="持久化存储目录")
@click.pass_context
def main(ctx: click.Context, storage_dir: str):
    ctx.ensure_object(dict)
    ctx.obj["storage"] = Storage(storage_dir)
    ctx.obj["storage_dir"] = storage_dir


@main.command(help="预检 (dry-run): 检查科目、版本、人数、源文件、目标名冲突")
@click.option("--config", "config_path", required=True, help="配置文件路径 (JSON)")
@click.option("--rooms", "csv_path", required=True, help="考场 CSV 清单路径")
@click.option("--batch-id", default=None, help="自定义批次 ID（默认自动生成）")
@click.option("--persist/--no-persist", default=True, show_default=True,
              help="是否将预检结果持久化（dry-run 模式建议开启；失败时也不会标为已完成）")
@click.pass_context
def precheck(ctx: click.Context, config_path: str, csv_path: str,
             batch_id: Optional[str], persist: bool):
    storage: Storage = ctx.obj["storage"]
    config = _load_config(ctx, config_path)
    rows = _load_rows(ctx, csv_path)

    batch = storage.create_batch(batch_id)
    if persist:
        batch.save_config_snapshot(config, csv_path)
        console.print(f"[info] 批次创建: [bold]{batch.batch_id}[/bold]")

    report, error = precheck_and_save(storage, batch, config, rows, persist=persist)
    _print_precheck_report(report, error)

    if error:
        ctx.exit(error.exit_code)
    ctx.exit(ExitCode.SUCCESS)


@main.command(help="发放: 按批次生成发放目录或 zip 包（需先通过预检）")
@click.option("--batch-id", required=True, help="批次 ID")
@click.pass_context
def dispatch(ctx: click.Context, batch_id: str):
    storage: Storage = ctx.obj["storage"]
    batch = storage.get_batch(batch_id)
    if not batch:
        err_console.print(f"批次不存在: {batch_id}")
        ctx.exit(ExitCode.BATCH_NOT_FOUND)

    if batch.status == BatchStatus.COMPLETED:
        err_console.print(f"批次已完成发放，如需重新发放请先回滚")
        ctx.exit(ExitCode.BATCH_ALREADY_DONE)

    if batch.status not in (BatchStatus.DRY_RUN_PASSED, BatchStatus.FAILED):
        if batch.status == BatchStatus.PENDING:
            err_console.print(f"批次尚未通过预检，请先执行 precheck")
        else:
            err_console.print(f"批次状态 {batch.status.value} 不允许发放")
        ctx.exit(ExitCode.BATCH_NOT_FOUND)

    precheck_report = batch.load_precheck_report()
    if not precheck_report:
        err_console.print("未找到预检报告，请先执行 precheck")
        ctx.exit(ExitCode.BATCH_NOT_FOUND)

    snap = batch.load_config_snapshot() or {}
    cfg_data = snap.get("config", {})
    try:
        config = DispatchConfig(**cfg_data)
    except Exception as e:
        err_console.print(f"配置快照损坏: {e}")
        ctx.exit(ExitCode.CONFIG_ERROR)

    items, error = run_dispatch_from_precheck(config, precheck_report, batch)

    success = sum(1 for i in items if i.dispatched)
    total = len(items)
    t = Table(title=f"发放结果 (成功 {success}/{total})")
    t.add_column("目标名")
    t.add_column("考场")
    t.add_column("科目")
    t.add_column("目标路径")
    t.add_column("状态", style="green")
    t.add_column("错误", style="red")
    for it in items:
        t.add_row(
            it.room_row.target_name,
            it.room_row.room_id,
            it.room_row.subject,
            it.target_path,
            "OK" if it.dispatched else "FAIL",
            it.error or "",
        )
    console.print(t)

    if error:
        err_console.print(f"退出码: {error.exit_code}")
        ctx.exit(error.exit_code)
    ctx.exit(ExitCode.SUCCESS)


@main.command(help="查询: 查看批次状态与详情")
@click.option("--batch-id", default=None, help="批次 ID（不传则列出所有批次）")
@click.option("--status", default=None, help="按状态过滤 (pending/dry_run_passed/completed/rolled_back/failed)")
@click.pass_context
def query(ctx: click.Context, batch_id: Optional[str], status: Optional[str]):
    storage: Storage = ctx.obj["storage"]

    if batch_id:
        data = query_batch(storage, batch_id)
        if not data:
            err_console.print(f"批次不存在: {batch_id}")
            ctx.exit(ExitCode.BATCH_NOT_FOUND)
        console.print_json(data=data)
    else:
        rows = list_batches(storage, status_filter=status)
        if not rows:
            console.print("[dim]无批次记录[/dim]")
            ctx.exit(ExitCode.SUCCESS)
        t = Table(title=f"批次列表 (共 {len(rows)} 个)")
        t.add_column("批次ID", style="bold")
        t.add_column("状态")
        t.add_column("创建时间")
        t.add_column("更新时间")
        t.add_column("项目数")
        t.add_column("成功/失败")
        t.add_column("备注")
        for r in rows:
            st_style = {
                "completed": "green",
                "dry_run_passed": "cyan",
                "rolled_back": "yellow",
                "failed": "red",
                "pending": "dim",
                "dispatching": "blue",
                "rolling_back": "magenta",
                "rollback_failed": "red",
            }.get(r["status"], "white")
            t.add_row(
                r["batch_id"],
                f"[{st_style}]{r['status']}[/{st_style}]",
                r["created_at"],
                r["updated_at"],
                str(r["total_items"]),
                f"{r['success_count']}/{r['fail_count']}",
                r.get("notes", "")[:40],
            )
        console.print(t)
    ctx.exit(ExitCode.SUCCESS)


@main.command(help="回滚: 删除已发放的文件（检测被篡改的目标文件并停止）")
@click.option("--batch-id", required=True, help="批次 ID")
@click.option("--force", is_flag=True, help="强制回滚（跳过 SHA256 校验）")
@click.pass_context
def rollback(ctx: click.Context, batch_id: str, force: bool):
    storage: Storage = ctx.obj["storage"]
    batch = storage.get_batch(batch_id)
    if not batch:
        err_console.print(f"批次不存在: {batch_id}")
        ctx.exit(ExitCode.BATCH_NOT_FOUND)

    results, error = rollback_batch(batch, force=force)

    t = Table(title="回滚结果")
    t.add_column("目标名")
    t.add_column("动作")
    t.add_column("说明")
    for r in results:
        action_style = {
            "deleted": "green",
            "skip": "dim",
            "blocked": "red",
            "error": "red",
        }.get(r["action"], "white")
        t.add_row(
            r.get("target_name", ""),
            f"[{action_style}]{r['action']}[/{action_style}]",
            r.get("reason", ""),
        )
    console.print(t)

    if error:
        err_console.print(f"\n[错误] {str(error)}")
        if error.details:
            for k, v in error.details.items():
                console.print(f"  {k}: {v}")
        err_console.print(f"退出码: {error.exit_code}")
        ctx.exit(error.exit_code)
    ctx.exit(ExitCode.SUCCESS)


@main.command(help="导出: 导出批次数据为 JSON 或 CSV")
@click.option("--format", "fmt", type=click.Choice(["json", "csv", "csv-items"]),
              default="json", show_default=True, help="导出格式")
@click.option("--batch-id", default=None, help="批次 ID（csv-items 必填）")
@click.option("--output", "output_path", required=True, help="输出文件路径")
@click.pass_context
def export(ctx: click.Context, fmt: str, batch_id: Optional[str], output_path: str):
    storage: Storage = ctx.obj["storage"]
    try:
        if fmt == "json":
            out = export_to_json(storage, output_path, batch_id)
        elif fmt == "csv":
            out = export_batches_csv(storage, output_path)
        elif fmt == "csv-items":
            if not batch_id:
                err_console.print("csv-items 格式需要 --batch-id")
                ctx.exit(ExitCode.INVALID_CSV)
            out = export_items_csv(storage, batch_id, output_path)
        console.print(f"[green]已导出到: {out}[/green]")
        ctx.exit(ExitCode.SUCCESS)
    except ValueError as e:
        err_console.print(str(e))
        ctx.exit(ExitCode.BATCH_NOT_FOUND)
    except Exception as e:
        err_console.print(f"导出失败: {e}")
        ctx.exit(ExitCode.IO_ERROR)


def run():
    try:
        main(standalone_mode=False)
    except click.exceptions.Exit as e:
        sys.exit(e.exit_code)
    except Exception as e:
        err_console.print(f"[未处理异常] {type(e).__name__}: {e}")
        sys.exit(ExitCode.UNKNOWN_ERROR)


if __name__ == "__main__":
    run()
