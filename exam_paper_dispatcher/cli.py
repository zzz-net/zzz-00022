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
    SignoffRow,
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
from .audit_pack import (
    AuditPackError,
    build_audit_pack,
    verify_audit_pack,
)
from .preview import run_import_preview
from .signoff import SignoffError, import_signoff

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


def _load_signoff_rows(ctx: click.Context, csv_path: str) -> list[SignoffRow]:
    try:
        return SignoffRow.from_csv(csv_path)
    except FileNotFoundError as e:
        err_console.print(f"[签收CSV错误] {e}")
        ctx.exit(ExitCode.INVALID_CSV)
    except ValueError as e:
        err_console.print(f"[签收CSV错误] {e}")
        ctx.exit(ExitCode.INVALID_CSV)
    except Exception as e:
        err_console.print(f"[签收CSV错误] 读取失败: {e}")
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


def _print_preview_report(report):
    title = "预演通过" if report.passed else "预演存在问题"
    style = "green" if report.passed else "yellow"
    p = Panel.fit(
        f"批次: [bold]{report.batch_id}[/bold]\n"
        f"预演ID: [bold]{report.preview_id}[/bold]\n"
        f"源文件根目录: {report.source_root_resolved}\n"
        f"输出目录: {report.output_root_resolved}\n"
        f"总行数: {report.total_rows}  |  有效: {report.valid_rows}\n"
        f"缺失源文件: {len(report.missing_sources)}  |  目标冲突: {len(report.target_conflicts)}\n"
        f"非法科目: {len(report.invalid_subjects)}  |  版本问题: {len(report.invalid_versions)}\n"
        f"潜在冲突: {len(report.potential_conflicts)}  |  警告: {len(report.warnings)}",
        title=f"[{style}]{title}[/{style}]",
        border_style=style,
    )
    console.print(p)

    if report.warnings:
        console.print("\n[bold yellow]警告列表:[/bold yellow]")
        for w in report.warnings:
            console.print(f"  - {w}")

    if report.preview_items:
        t = Table(title=f"预演明细 (共 {len(report.preview_items)} 项)", show_lines=True)
        t.add_column("考场", style="bold")
        t.add_column("科目")
        t.add_column("版本")
        t.add_column("人数")
        t.add_column("源文件")
        t.add_column("源路径")
        t.add_column("目标文件名")
        t.add_column("目标路径")
        for it in report.preview_items:
            src_exist_style = "green" if it["source_exists"] else "red"
            tgt_exist_style = "yellow" if it["target_already_exists"] else "white"
            t.add_row(
                it["room_id"],
                it["subject"],
                it.get("version", ""),
                str(it["students_count"]),
                f"[{src_exist_style}]{it['source_file']}[/{src_exist_style}]",
                it["source_path_resolved"],
                it["target_name"],
                f"[{tgt_exist_style}]{it['target_path_resolved']}[/{tgt_exist_style}]",
            )
        console.print(t)

    if report.potential_conflicts:
        t = Table(title="潜在冲突", show_lines=True)
        t.add_column("冲突类型", style="red")
        t.add_column("目标路径", style="yellow")
        t.add_column("涉及考场/科目")
        for c in report.potential_conflicts:
            items_desc = ", ".join(
                f"{r['room_id']}/{r['subject']}({r['target_name']})"
                for r in c["items"]
            )
            t.add_row(c["type"], c["target_path"], items_desc)
        console.print(t)

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


def _print_signoff_report(report, error: Optional[SignoffError]):
    title = "签收导入成功" if report.passed else "签收导入失败"
    style = "green" if report.passed else "red"
    p = Panel.fit(
        f"批次: [bold]{report.batch_id}[/bold]\n"
        f"签收ID: [bold]{report.signoff_id}[/bold]\n"
        f"总行数: {report.total_rows}  |  有效: {report.valid_rows}\n"
        f"已签收考场: {report.signed_rooms}  |  异常数: {report.abnormal_count}\n"
        f"考场不在批次: {len(report.invalid_rooms)}  |  份数不匹配: {len(report.count_mismatches)}\n"
        f"冲突项: {len(report.conflicts)}",
        title=f"[{style}]{title}[/{style}]",
        border_style=style,
    )
    console.print(p)

    if report.signoff_items:
        t = Table(title=f"签收明细 (共 {len(report.signoff_items)} 项)", show_lines=True)
        t.add_column("考场", style="bold")
        t.add_column("科目")
        t.add_column("签收人")
        t.add_column("签收时间")
        t.add_column("实收份数")
        t.add_column("缺损说明")
        t.add_column("备注")
        t.add_column("异常")
        for it in report.signoff_items:
            abn_style = "red" if it.get("is_abnormal") else "white"
            t.add_row(
                it["room_id"],
                it["subject"],
                it["signoff_person"],
                it["signoff_time"],
                str(it["received_count"]),
                it.get("damage_note", ""),
                it.get("remark", ""),
                f"[{abn_style}]{'是' if it.get('is_abnormal') else '否'}[/{abn_style}]",
            )
        console.print(t)

    if report.invalid_rooms:
        t = Table(title="考场不在批次中", show_lines=True)
        t.add_column("行号", style="dim")
        t.add_column("考试ID")
        t.add_column("考场")
        t.add_column("科目")
        t.add_column("说明", style="red")
        for m in report.invalid_rooms:
            t.add_row(
                str(m.get("line_no", "")),
                m.get("exam_id", ""),
                m.get("room_id", ""),
                m.get("subject", ""),
                m.get("message", ""),
            )
        console.print(t)

    if report.count_mismatches:
        t = Table(title="份数不匹配", show_lines=True)
        t.add_column("行号", style="dim")
        t.add_column("考场")
        t.add_column("科目")
        t.add_column("期望份数", style="cyan")
        t.add_column("实收份数", style="red")
        for m in report.count_mismatches:
            t.add_row(
                str(m.get("line_no", "")),
                m.get("room_id", ""),
                m.get("subject", ""),
                str(m.get("expected", "")),
                str(m.get("received", "")),
            )
        console.print(t)

    if report.conflicts:
        existing = [c for c in report.conflicts if c.get("type") == "existing_signoff"]
        dup_csv = [c for c in report.conflicts if c.get("type") == "duplicate_in_csv"]
        if existing:
            t = Table(title="重复签收冲突（需 --force 确认更新）", show_lines=True)
            t.add_column("考场")
            t.add_column("科目")
            t.add_column("原签收人")
            t.add_column("原签收时间")
            t.add_column("新签收人")
            t.add_column("新签收时间")
            for c in existing:
                t.add_row(
                    c.get("room_id", ""),
                    c.get("subject", ""),
                    c.get("old_signoff_person", ""),
                    c.get("old_signoff_time", ""),
                    c.get("new_signoff_person", ""),
                    c.get("new_signoff_time", ""),
                )
            console.print(t)
        if dup_csv:
            t = Table(title="CSV 内重复考场", show_lines=True)
            t.add_column("考场")
            t.add_column("科目")
            t.add_column("涉及行号")
            for c in dup_csv:
                t.add_row(
                    c.get("room_id", ""),
                    c.get("subject", ""),
                    ", ".join(str(l) for l in c.get("lines", [])),
                )
            console.print(t)

    if not report.passed and error:
        err_console.print(f"[red]错误: {error.message}[/red]")
        err_console.print(f"退出码: {error.exit_code}")


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

    try:
        batch = storage.create_batch(batch_id)
    except ValueError as e:
        err_console.print(str(e))
        ctx.exit(ExitCode.BATCH_ID_CONFLICT)
    if persist:
        batch.save_config_snapshot(config, csv_path)
        console.print(f"[info] 批次创建: [bold]{batch.batch_id}[/bold]")

    report, error = precheck_and_save(storage, batch, config, rows, persist=persist)
    _print_precheck_report(report, error)

    if error:
        ctx.exit(error.exit_code)
    ctx.exit(ExitCode.SUCCESS)


@main.command(
    help="导入预演: 汇总即将创建的批次、考场使用的源文件、目标文件名、版本、"
         "人数校验结果和潜在冲突，但不写出任何试卷包。结果按批次保存，重启后仍可查询。"
)
@click.option("--config", "config_path", required=True, help="配置文件路径 (JSON)")
@click.option("--rooms", "csv_path", required=True, help="考场 CSV 清单路径")
@click.option("--batch-id", default=None, help="自定义批次 ID（默认自动生成；如已存在则追加新预演，不覆盖旧记录）")
@click.pass_context
def preview(ctx: click.Context, config_path: str, csv_path: str, batch_id: Optional[str]):
    storage: Storage = ctx.obj["storage"]
    config = _load_config(ctx, config_path)
    rows = _load_rows(ctx, csv_path)

    batch = storage.get_batch(batch_id) if batch_id else None
    if batch is None:
        try:
            batch = storage.create_batch(batch_id)
        except ValueError as e:
            err_console.print(str(e))
            ctx.exit(ExitCode.BATCH_ID_CONFLICT)
        console.print(f"[info] 批次创建: [bold]{batch.batch_id}[/bold]")
    else:
        console.print(f"[info] 追加预演到已有批次: [bold]{batch.batch_id}[/bold]（已有 {len(batch.list_preview_ids())} 次预演记录）")

    report = run_import_preview(storage, batch, config, rows, csv_path, config_path)
    _print_preview_report(report)

    if not report.passed:
        console.print(f"\n[yellow]预演发现问题，详情见上方表格。[/yellow]")
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
        t.add_column("批次ID", style="bold", overflow="fold")
        t.add_column("状态", overflow="fold")
        t.add_column("创建时间", overflow="fold")
        t.add_column("更新时间", overflow="fold")
        t.add_column("项目", overflow="fold")
        t.add_column("成/败", overflow="fold")
        t.add_column("签收", overflow="fold")
        t.add_column("异常", overflow="fold")
        t.add_column("最后签收", overflow="fold")
        t.add_column("备注", overflow="fold")
        for r in rows:
            st_style = {
                "completed": "green",
                "dry_run_passed": "cyan",
                "preview": "bright_cyan",
                "rolled_back": "yellow",
                "failed": "red",
                "pending": "dim",
                "dispatching": "blue",
                "rolling_back": "magenta",
                "rollback_failed": "red",
            }.get(r["status"], "white")
            signoff_status = r.get("signoff_status", "none")
            signoff_style = {
                "complete": "green",
                "partial": "yellow",
                "none": "dim",
            }.get(signoff_status, "white")
            t.add_row(
                r["batch_id"],
                f"[{st_style}]{r['status']}[/{st_style}]",
                r["created_at"][:19],
                r["updated_at"][:19],
                str(r["total_items"]),
                f"{r['success_count']}/{r['fail_count']}",
                f"[{signoff_style}]{signoff_status}[/{signoff_style}]",
                str(r.get("signoff_abnormal_count", 0)),
                str(r.get("signoff_last_imported_at", ""))[:19],
                r.get("notes", "")[:30],
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
    except click.exceptions.Exit:
        raise
    except Exception as e:
        err_console.print(f"导出失败: {e}")
        ctx.exit(ExitCode.IO_ERROR)


@main.command(
    help="发放签收核销: 导入签收 CSV，按批次记录每个考场的签收人、时间、"
         "实收份数、缺损说明和备注，生成核销摘要。"
         "导入时校验批次已发放、考场属于该批次、份数与预检人数匹配；"
         "同一考场重复导入不会静默覆盖，需加 --force 确认更新。"
)
@click.option("--batch-id", required=True, help="批次 ID（必须已完成发放）")
@click.option("--signoffs", "csv_path", required=True, help="签收 CSV 清单路径")
@click.option("--force", is_flag=True,
              help="强制覆盖已存在的考场签收记录（默认拒绝重复导入并提示冲突）")
@click.pass_context
def signoff(ctx: click.Context, batch_id: str, csv_path: str, force: bool):
    storage: Storage = ctx.obj["storage"]
    batch = storage.get_batch(batch_id)
    if not batch:
        err_console.print(f"批次不存在: {batch_id}")
        ctx.exit(ExitCode.BATCH_NOT_FOUND)

    rows = _load_signoff_rows(ctx, csv_path)

    report, error = import_signoff(storage, batch, rows, csv_path, force=force)
    _print_signoff_report(report, error)

    if error:
        ctx.exit(error.exit_code)
    ctx.exit(ExitCode.SUCCESS)


@main.command(
    help="生成交接审计包: 收集配置快照、预检报告、发放明细、回滚记录、"
         "事件日志和 README，打包为可离线交接的 zip。重启 CLI 后可从持久化状态重新生成。"
)
@click.option("--batch-id", required=True, help="批次 ID（需已完成至少一次预检）")
@click.option("--output", "output_path", required=True, help="输出 zip 文件路径，如 audit_batch-001.zip")
@click.option("--force", is_flag=True, help="覆盖已存在的输出文件（默认拒绝同名输出）")
@click.pass_context
def audit_pack(ctx: click.Context, batch_id: str, output_path: str, force: bool):
    storage: Storage = ctx.obj["storage"]
    out, error = build_audit_pack(storage, batch_id, output_path, force=force)
    if error:
        err_console.print(f"[审计包错误] {str(error)}")
        if error.details:
            for k, v in error.details.items():
                console.print(f"  {k}: {v}")
        ctx.exit(error.exit_code)
    console.print(f"[green]审计包已生成: {out}[/green]")
    ctx.exit(ExitCode.SUCCESS)


@main.command(
    help="校验交接审计包: 读取归档并校验 manifest、每个文件的 SHA256、"
         "批次号、配置摘要以及预检/发放/回滚明细数量，发现篡改或缺失时逐条指出差异。"
)
@click.option("--archive", "archive_path", required=True, help="审计包 zip 文件路径")
@click.pass_context
def audit_verify(ctx: click.Context, archive_path: str):
    result = verify_audit_pack(archive_path)
    if result.ok:
        m = result.manifest or {}
        t = Table(title=f"审计包校验通过", show_lines=True)
        t.add_column("项目")
        t.add_column("值", style="green")
        t.add_row("归档文件", str(result.archive_path))
        t.add_row("批次 ID", m.get("batch_id", ""))
        t.add_row("批次状态", m.get("batch_status", ""))
        t.add_row("Schema 版本", m.get("schema_version", ""))
        counts = m.get("counts", {})
        t.add_row("预检明细数", str(counts.get("precheck_items", 0)))
        t.add_row("发放明细数", str(counts.get("dispatch_items", 0)))
        t.add_row("回滚记录数", str(counts.get("rollback_results", 0)))
        t.add_row("包含文件数", str(len(m.get("files_sha256", {}))))
        console.print(t)
        ctx.exit(ExitCode.SUCCESS)
    else:
        err_console.print(f"[red]审计包校验失败: {archive_path}[/red]")
        for e in result.errors:
            err_console.print(f"  - {e}")
        ctx.exit(ExitCode.AUDIT_VERIFY_FAILED)


def run():
    try:
        rv = main(standalone_mode=False)
        if isinstance(rv, int) and rv != 0:
            sys.exit(rv)
    except click.exceptions.Exit as e:
        sys.exit(e.exit_code)
    except Exception as e:
        err_console.print(f"[未处理异常] {type(e).__name__}: {e}")
        sys.exit(ExitCode.UNKNOWN_ERROR)


if __name__ == "__main__":
    run()
