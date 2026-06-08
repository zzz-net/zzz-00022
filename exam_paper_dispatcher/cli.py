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
from .signoff import (
    SignoffError,
    import_signoff,
    correct_signoff,
    revoke_signoff,
    get_signoff_history,
    CORRECTABLE_FIELDS,
)
from .incident import (
    IncidentError,
    create_incident,
    handle_incident,
    close_incident,
    list_incidents,
    get_incident_detail,
    build_incident_summary,
)
from .models import (
    IncidentStatus,
    IncidentType,
    INCIDENT_TYPE_LABELS,
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


def _print_correct_result(result: dict, error: Optional[SignoffError]):
    if error:
        err_console.print(f"[red]签收更正失败: {error.message}[/red]")
        if error.details:
            for k, v in error.details.items():
                console.print(f"  {k}: {v}")
        err_console.print(f"退出码: {error.exit_code}")
        return

    if not result.get("changed", False):
        console.print(f"[yellow]无需更正: {result.get('message', '')}[/yellow]")
        return

    p = Panel.fit(
        f"批次: [bold]{result.get('current', {}).get('exam_id', '')}[/bold]\n"
        f"考场: [bold]{result.get('current', {}).get('room_id', '')}/{result.get('current', {}).get('subject', '')}[/bold]\n"
        f"审计ID: [bold]{result.get('audit_id', '')}[/bold]\n"
        f"版本: {result.get('version_before', 0)} → {result.get('version_after', 0)}",
        title="[green]签收更正成功[/green]",
        border_style="green",
    )
    console.print(p)

    t = Table(title="字段变更明细", show_lines=True)
    t.add_column("字段", style="bold")
    t.add_column("原值", style="red")
    t.add_column("新值", style="green")
    for field in result.get("old_values", {}):
        old_v = str(result["old_values"].get(field, ""))
        new_v = str(result["new_values"].get(field, ""))
        t.add_row(field, old_v, new_v)
    console.print(t)

    curr = result.get("current", {})
    if curr:
        t2 = Table(title="当前签收状态", show_lines=True)
        t2.add_column("字段")
        t2.add_column("值")
        for k, v in curr.items():
            if k not in ("line_no", "expected_count", "is_abnormal"):
                t2.add_row(str(k), str(v))
        console.print(t2)


def _print_revoke_result(result: dict, error: Optional[SignoffError]):
    if error:
        err_console.print(f"[red]签收撤销失败: {error.message}[/red]")
        if error.details:
            for k, v in error.details.items():
                console.print(f"  {k}: {v}")
        err_console.print(f"退出码: {error.exit_code}")
        return

    p = Panel.fit(
        f"批次: [bold]{result.get('exam_id', '')}[/bold]\n"
        f"考场: [bold]{result.get('room_id', '')}/{result.get('subject', '')}[/bold]\n"
        f"审计ID: [bold]{result.get('audit_id', '')}[/bold]\n"
        f"撤销时间: {result.get('revoked_at', '')}\n"
        f"操作人: {result.get('revoked_by', '')}\n"
        f"撤销原因: {result.get('revoke_reason', '')}\n"
        f"版本: {result.get('version_before', 0)} → {result.get('version_after', 0)}",
        title="[green]签收撤销成功[/green]",
        border_style="green",
    )
    console.print(p)
    console.print("[yellow]提示: 批次发放状态保持不变，仅该考场的签收标记为已撤销。[/yellow]")


def _print_signoff_history(history_list: list[dict]):
    if not history_list:
        console.print("[dim]无签收历史记录[/dim]")
        return

    for h in history_list:
        status_style = "yellow" if h.get("revoked") else "green"
        status_text = "已撤销" if h.get("revoked") else "有效"
        imported_text = "已导入" if h.get("imported") else "未导入"
        p = Panel.fit(
            f"考场: [bold]{h.get('room_id', '')}/{h.get('subject', '')}[/bold] "
            f"([{status_style}]{status_text}[/{status_style}])\n"
            f"考试ID: {h.get('exam_id', '')}\n"
            f"导入状态: {imported_text}\n"
            f"当前版本: {h.get('current_version', 0)}",
            title="签收历史",
            border_style="cyan",
        )
        console.print(p)

        if h.get("import_records"):
            t_import = Table(title=f"导入记录 ({len(h['import_records'])} 条)", show_lines=True)
            t_import.add_column("签收ID")
            t_import.add_column("导入时间")
            t_import.add_column("签收人")
            t_import.add_column("签收时间")
            t_import.add_column("实收份数")
            for imp in h["import_records"]:
                vals = imp.get("values", {})
                t_import.add_row(
                    imp.get("signoff_id", ""),
                    imp.get("imported_at", "")[:19],
                    vals.get("signoff_person", ""),
                    vals.get("signoff_time", ""),
                    str(vals.get("received_count", "")),
                )
            console.print(t_import)

        if h.get("audit_records"):
            t_audit = Table(title=f"审计记录 ({len(h['audit_records'])} 条)", show_lines=True)
            t_audit.add_column("审计ID")
            t_audit.add_column("时间")
            t_audit.add_column("动作")
            t_audit.add_column("操作人")
            t_audit.add_column("版本")
            t_audit.add_column("原因")
            for aud in h["audit_records"]:
                t_audit.add_row(
                    aud.get("audit_id", ""),
                    aud.get("timestamp", "")[:19],
                    aud.get("action", ""),
                    aud.get("operator", ""),
                    f"{aud.get('version_before', 0)}→{aud.get('version_after', 0)}",
                    aud.get("reason", ""),
                )
            console.print(t_audit)

        if h.get("current"):
            curr = h["current"]
            t_curr = Table(title="当前有效状态", show_lines=True)
            t_curr.add_column("字段")
            t_curr.add_column("值")
            for k, v in curr.items():
                if k not in ("line_no", "expected_count"):
                    t_curr.add_row(str(k), str(v))
            console.print(t_curr)


def _print_incident_created(ticket, error: Optional[IncidentError]):
    if error:
        err_console.print(f"[red]创建异常处置单失败: {error.message}[/red]")
        if error.details:
            for k, v in error.details.items():
                console.print(f"  {k}: {v}")
        err_console.print(f"退出码: {error.exit_code}")
        return

    p = Panel.fit(
        f"处置单ID: [bold]{ticket.ticket_id}[/bold]\n"
        f"批次: [bold]{ticket.batch_id}[/bold]\n"
        f"考场: [bold]{ticket.exam_id}/{ticket.room_id}/{ticket.subject}[/bold]\n"
        f"问题类型: [bold]{INCIDENT_TYPE_LABELS.get(ticket.incident_type, ticket.incident_type.value)}[/bold]\n"
        f"操作人: {ticket.operator}\n"
        f"创建时间: {ticket.created_at}\n"
        f"状态: [yellow]{ticket.status.value}[/yellow]",
        title="[green]异常处置单创建成功[/green]",
        border_style="green",
    )
    console.print(p)

    if ticket.description:
        console.print(f"\n[bold]问题说明:[/bold]\n  {ticket.description}")
    if ticket.attachment_paths:
        t_att = Table(title=f"附件 ({len(ticket.attachment_paths)} 个)", show_lines=True)
        t_att.add_column("#")
        t_att.add_column("路径")
        for i, p in enumerate(ticket.attachment_paths, 1):
            t_att.add_row(str(i), p)
        console.print(t_att)


def _print_incident_list(tickets: list):
    if not tickets:
        console.print("[dim]无异常处置单记录[/dim]")
        return

    t = Table(title=f"异常处置单列表 (共 {len(tickets)} 个)", show_lines=True)
    t.add_column("处置单ID", style="bold", overflow="fold")
    t.add_column("批次ID", overflow="fold")
    t.add_column("考场", overflow="fold")
    t.add_column("科目", overflow="fold")
    t.add_column("类型", overflow="fold")
    t.add_column("状态", overflow="fold")
    t.add_column("操作人", overflow="fold")
    t.add_column("创建时间", overflow="fold")
    t.add_column("处理记录", overflow="fold")
    t.add_column("关闭时间", overflow="fold")

    for ticket in tickets:
        status_style = {
            "open": "yellow",
            "processing": "cyan",
            "closed": "green",
        }.get(ticket.status.value, "white")
        t.add_row(
            ticket.ticket_id,
            ticket.batch_id,
            f"{ticket.exam_id}/{ticket.room_id}",
            ticket.subject,
            INCIDENT_TYPE_LABELS.get(ticket.incident_type, ticket.incident_type.value),
            f"[{status_style}]{ticket.status.value}[/{status_style}]",
            ticket.operator,
            ticket.created_at[:19],
            str(len(ticket.handling_records)),
            (ticket.closed_at or "")[:19],
        )
    console.print(t)


def _print_incident_detail(detail: dict):
    status_style = {
        "open": "yellow",
        "processing": "cyan",
        "closed": "green",
    }.get(detail["status"], "white")

    p = Panel.fit(
        f"处置单ID: [bold]{detail['ticket_id']}[/bold]\n"
        f"批次: [bold]{detail['batch_id']}[/bold]\n"
        f"考场: [bold]{detail['exam_id']}/{detail['room_id']}/{detail['subject']}[/bold]\n"
        f"问题类型: [bold]{detail['incident_type_label']}[/bold]\n"
        f"操作人: {detail['operator']}\n"
        f"创建时间: {detail['created_at']}\n"
        f"状态: [{status_style}]{detail['status']}[/{status_style}]\n"
        f"处理记录数: {detail['handling_count']}"
        + (f"\n关闭时间: {detail['closed_at']}" if detail.get("closed_at") else "")
        + (f"\n关闭人: {detail['closed_by']}" if detail.get("closed_by") else "")
        + (f"\n关闭原因: {detail['close_reason']}" if detail.get("close_reason") else ""),
        title="异常处置单详情",
        border_style="cyan",
    )
    console.print(p)

    console.print(f"\n[bold]问题说明:[/bold]\n  {detail['description']}")

    if detail.get("attachment_paths"):
        t_att = Table(title=f"附件 ({len(detail['attachment_paths'])} 个)", show_lines=True)
        t_att.add_column("#")
        t_att.add_column("路径")
        for i, p in enumerate(detail["attachment_paths"], 1):
            t_att.add_row(str(i), p)
        console.print(t_att)

    if detail.get("handling_records"):
        t_rec = Table(title=f"处理记录 ({len(detail['handling_records'])} 条)", show_lines=True)
        t_rec.add_column("时间")
        t_rec.add_column("操作人")
        t_rec.add_column("处理动作")
        t_rec.add_column("备注")
        for rec in detail["handling_records"]:
            t_rec.add_row(
                rec["timestamp"][:19],
                rec["operator"],
                rec["action"],
                rec.get("note", ""),
            )
        console.print(t_rec)


def _print_incident_handled(ticket, error: Optional[IncidentError]):
    if error:
        err_console.print(f"[red]处理异常处置单失败: {error.message}[/red]")
        if error.details:
            for k, v in error.details.items():
                console.print(f"  {k}: {v}")
        err_console.print(f"退出码: {error.exit_code}")
        return

    if ticket is None:
        return

    status_style = {
        "open": "yellow",
        "processing": "cyan",
        "closed": "green",
    }.get(ticket.status.value, "white")

    p = Panel.fit(
        f"处置单ID: [bold]{ticket.ticket_id}[/bold]\n"
        f"状态: [{status_style}]{ticket.status.value}[/{status_style}]\n"
        f"处理记录数: {len(ticket.handling_records)}",
        title="[green]处理记录已追加[/green]",
        border_style="green",
    )
    console.print(p)

    latest = ticket.handling_records[-1]
    t_latest = Table(title="最新处理记录", show_lines=True)
    t_latest.add_column("字段")
    t_latest.add_column("值")
    t_latest.add_row("时间", latest.timestamp)
    t_latest.add_row("操作人", latest.operator)
    t_latest.add_row("处理动作", latest.action)
    if latest.note:
        t_latest.add_row("备注", latest.note)
    console.print(t_latest)


def _print_incident_closed(ticket, error: Optional[IncidentError]):
    if error:
        err_console.print(f"[red]关闭异常处置单失败: {error.message}[/red]")
        if error.details:
            for k, v in error.details.items():
                console.print(f"  {k}: {v}")
        err_console.print(f"退出码: {error.exit_code}")
        return

    if ticket is None:
        return

    p = Panel.fit(
        f"处置单ID: [bold]{ticket.ticket_id}[/bold]\n"
        f"关闭时间: {ticket.closed_at}\n"
        f"关闭人: {ticket.closed_by}"
        + (f"\n关闭原因: {ticket.close_reason}" if ticket.close_reason else ""),
        title="[green]异常处置单已关闭[/green]",
        border_style="green",
    )
    console.print(p)


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
        t.add_column("签收异", overflow="fold")
        t.add_column("更/撤", overflow="fold")
        t.add_column("处置单", overflow="fold")
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
            audit_info = f"{r.get('signoff_corrected_count', 0)}/{r.get('signoff_revoked_count', 0)}"
            incident_open = r.get("incident_open_count", 0) + r.get("incident_processing_count", 0)
            incident_closed = r.get("incident_closed_count", 0)
            incident_style = "yellow" if incident_open > 0 else "dim"
            t.add_row(
                r["batch_id"],
                f"[{st_style}]{r['status']}[/{st_style}]",
                r["created_at"][:19],
                r["updated_at"][:19],
                str(r["total_items"]),
                f"{r['success_count']}/{r['fail_count']}",
                f"[{signoff_style}]{signoff_status}[/{signoff_style}]",
                str(r.get("signoff_abnormal_count", 0)),
                audit_info,
                f"[{incident_style}]{incident_open}/{incident_closed}[/{incident_style}]",
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
    help="更正签收记录: 按批次和考场定位到具体签收记录，更正签收人、时间、"
         "实收份数、缺损说明或备注。每次更正写入审计日志，保留操作者、原因、"
         "时间、原值和新值，版本号自动递增。允许字段: "
         + ", ".join(sorted(CORRECTABLE_FIELDS))
)
@click.option("--batch-id", required=True, help="批次 ID（必须已完成发放）")
@click.option("--exam-id", required=True, help="考试 ID")
@click.option("--room-id", required=True, help="考场 ID")
@click.option("--subject", required=True, help="科目")
@click.option("--operator", required=True, help="操作人姓名/工号（写入审计日志）")
@click.option("--reason", required=True, help="更正原因（写入审计日志）")
@click.option("--signoff-person", default=None, help="更正: 签收人")
@click.option("--signoff-time", default=None, help="更正: 签收时间")
@click.option("--received-count", type=int, default=None, help="更正: 实收份数")
@click.option("--damage-note", default=None, help="更正: 缺损说明")
@click.option("--remark", default=None, help="更正: 备注")
@click.pass_context
def signoff_correct(
    ctx: click.Context,
    batch_id: str,
    exam_id: str,
    room_id: str,
    subject: str,
    operator: str,
    reason: str,
    signoff_person: Optional[str],
    signoff_time: Optional[str],
    received_count: Optional[int],
    damage_note: Optional[str],
    remark: Optional[str],
):
    storage: Storage = ctx.obj["storage"]
    batch = storage.get_batch(batch_id)
    if not batch:
        err_console.print(f"批次不存在: {batch_id}")
        ctx.exit(ExitCode.BATCH_NOT_FOUND)

    updates: dict = {}
    if signoff_person is not None:
        updates["signoff_person"] = signoff_person
    if signoff_time is not None:
        updates["signoff_time"] = signoff_time
    if received_count is not None:
        updates["received_count"] = received_count
    if damage_note is not None:
        updates["damage_note"] = damage_note
    if remark is not None:
        updates["remark"] = remark

    result, error = correct_signoff(
        batch=batch,
        exam_id=exam_id,
        room_id=room_id,
        subject=subject,
        updates=updates,
        operator=operator,
        reason=reason,
    )
    _print_correct_result(result, error)
    if error:
        ctx.exit(error.exit_code)
    ctx.exit(ExitCode.SUCCESS)


@main.command(
    help="撤销签收记录: 按批次和考场撤销单个考场的签收记录（不改变批次发放状态）。"
         "撤销写入审计日志，保留操作者、原因、时间；撤销后该考场视为未签收，"
         "可通过重新导入签收 CSV 或 signoff-correct 重新登记。"
)
@click.option("--batch-id", required=True, help="批次 ID（必须已完成发放）")
@click.option("--exam-id", required=True, help="考试 ID")
@click.option("--room-id", required=True, help="考场 ID")
@click.option("--subject", required=True, help="科目")
@click.option("--operator", required=True, help="操作人姓名/工号（写入审计日志）")
@click.option("--reason", required=True, help="撤销原因（写入审计日志）")
@click.pass_context
def signoff_revoke(
    ctx: click.Context,
    batch_id: str,
    exam_id: str,
    room_id: str,
    subject: str,
    operator: str,
    reason: str,
):
    storage: Storage = ctx.obj["storage"]
    batch = storage.get_batch(batch_id)
    if not batch:
        err_console.print(f"批次不存在: {batch_id}")
        ctx.exit(ExitCode.BATCH_NOT_FOUND)

    result, error = revoke_signoff(
        batch=batch,
        exam_id=exam_id,
        room_id=room_id,
        subject=subject,
        operator=operator,
        reason=reason,
    )
    _print_revoke_result(result, error)
    if error:
        ctx.exit(error.exit_code)
    ctx.exit(ExitCode.SUCCESS)


@main.command(
    help="签收历史查询: 按批次（和可选的考场/科目）查看签收导入记录、"
         "更正/撤销审计日志、当前有效签收状态和版本号。重启 CLI 后结果一致。"
)
@click.option("--batch-id", required=True, help="批次 ID")
@click.option("--exam-id", default=None, help="按考试 ID 过滤（可选）")
@click.option("--room-id", default=None, help="按考场 ID 过滤（可选）")
@click.option("--subject", default=None, help="按科目过滤（可选）")
@click.option("--format", "fmt", type=click.Choice(["table", "json"]),
              default="table", show_default=True, help="输出格式")
@click.pass_context
def signoff_history(
    ctx: click.Context,
    batch_id: str,
    exam_id: Optional[str],
    room_id: Optional[str],
    subject: Optional[str],
    fmt: str,
):
    storage: Storage = ctx.obj["storage"]
    batch = storage.get_batch(batch_id)
    if not batch:
        err_console.print(f"批次不存在: {batch_id}")
        ctx.exit(ExitCode.BATCH_NOT_FOUND)

    history = get_signoff_history(
        batch=batch,
        exam_id=exam_id,
        room_id=room_id,
        subject=subject,
    )
    if fmt == "json":
        console.print_json(data=history)
    else:
        _print_signoff_history(history)
    ctx.exit(ExitCode.SUCCESS)


@main.command(
    help="创建异常处置单: 批次完成发放后登记试卷包损坏、错装、临时换考场等问题。"
         "只有已发放 (completed) 或已回滚 (rolled_back) 的批次才能建单。"
         "同一考场存在未关闭工单时将提示冲突。"
         "每次创建、处理、关闭操作都会写入独立的审计日志。"
)
@click.option("--batch-id", required=True, help="批次 ID")
@click.option("--exam-id", required=True, help="考试 ID")
@click.option("--room-id", required=True, help="考场 ID")
@click.option("--subject", required=True, help="科目")
@click.option(
    "--type", "incident_type",
    type=click.Choice(["package_damaged", "wrong_package", "room_change", "other"]),
    required=True,
    help="问题类型: package_damaged(试卷包损坏)/wrong_package(错装)/room_change(临时换考场)/other(其他)",
)
@click.option("--description", required=True, help="问题说明")
@click.option("--operator", required=True, help="操作人姓名/工号")
@click.option("--attachment", "attachments", multiple=True, help="附件路径（可多次指定）")
@click.pass_context
def incident_create(
    ctx: click.Context,
    batch_id: str,
    exam_id: str,
    room_id: str,
    subject: str,
    incident_type: str,
    description: str,
    operator: str,
    attachments: tuple[str, ...],
):
    storage: Storage = ctx.obj["storage"]
    batch = storage.get_batch(batch_id)
    if not batch:
        err_console.print(f"批次不存在: {batch_id}")
        ctx.exit(ExitCode.BATCH_NOT_FOUND)

    type_map = {
        "package_damaged": IncidentType.PACKAGE_DAMAGED,
        "wrong_package": IncidentType.WRONG_PACKAGE,
        "room_change": IncidentType.ROOM_CHANGE,
        "other": IncidentType.OTHER,
    }
    itype = type_map[incident_type]

    ticket, error = create_incident(
        storage=storage,
        batch=batch,
        exam_id=exam_id,
        room_id=room_id,
        subject=subject,
        incident_type=itype,
        description=description,
        operator=operator,
        attachment_paths=list(attachments),
    )
    _print_incident_created(ticket, error)
    if error:
        ctx.exit(error.exit_code)
    ctx.exit(ExitCode.SUCCESS)


@main.command(
    help="查看异常处置单: 列出批次的处置单或查看单个处置单详情。"
         "可按状态过滤（open/processing/closed）。"
)
@click.option("--batch-id", required=True, help="批次 ID")
@click.option("--ticket-id", default=None, help="处置单 ID（不传则列出该批次所有处置单）")
@click.option(
    "--status", "status_filter",
    type=click.Choice(["open", "processing", "closed"]),
    default=None,
    help="按状态过滤",
)
@click.option("--format", "fmt", type=click.Choice(["table", "json"]),
              default="table", show_default=True, help="输出格式")
@click.pass_context
def incident_list(
    ctx: click.Context,
    batch_id: str,
    ticket_id: Optional[str],
    status_filter: Optional[str],
    fmt: str,
):
    storage: Storage = ctx.obj["storage"]
    batch = storage.get_batch(batch_id)
    if not batch:
        err_console.print(f"批次不存在: {batch_id}")
        ctx.exit(ExitCode.BATCH_NOT_FOUND)

    if ticket_id:
        detail = get_incident_detail(batch, ticket_id)
        if not detail:
            err_console.print(f"异常处置单不存在: {ticket_id}")
            ctx.exit(ExitCode.INCIDENT_NOT_FOUND)
        if fmt == "json":
            console.print_json(data=detail)
        else:
            _print_incident_detail(detail)
    else:
        status = IncidentStatus(status_filter) if status_filter else None
        tickets = list_incidents(batch, status_filter=status)
        if fmt == "json":
            console.print_json(data=[get_incident_detail(batch, t.ticket_id) for t in tickets])
        else:
            _print_incident_list(tickets)
    ctx.exit(ExitCode.SUCCESS)


@main.command(
    help="处理异常处置单: 追加处理记录，可选择更新状态（processing 或保持 open）。"
         "已关闭的处置单无法追加处理记录。"
         "每次处理操作都会写入独立的审计日志。"
)
@click.option("--batch-id", required=True, help="批次 ID")
@click.option("--ticket-id", required=True, help="处置单 ID")
@click.option("--operator", required=True, help="操作人姓名/工号")
@click.option("--action", required=True, help="处理动作描述")
@click.option("--note", default="", help="处理备注")
@click.option(
    "--to-status", "to_status",
    type=click.Choice(["open", "processing"]),
    default=None,
    help="更新状态（可选：open/processing）",
)
@click.pass_context
def incident_handle(
    ctx: click.Context,
    batch_id: str,
    ticket_id: str,
    operator: str,
    action: str,
    note: str,
    to_status: Optional[str],
):
    storage: Storage = ctx.obj["storage"]
    batch = storage.get_batch(batch_id)
    if not batch:
        err_console.print(f"批次不存在: {batch_id}")
        ctx.exit(ExitCode.BATCH_NOT_FOUND)

    new_status = IncidentStatus(to_status) if to_status else None
    ticket, error = handle_incident(
        batch=batch,
        ticket_id=ticket_id,
        operator=operator,
        action=action,
        note=note,
        new_status=new_status,
    )
    _print_incident_handled(ticket, error)
    if error:
        ctx.exit(error.exit_code)
    ctx.exit(ExitCode.SUCCESS)


@main.command(
    help="关闭异常处置单: 将处置单状态标记为 closed，记录关闭人和关闭原因。"
         "已关闭的处置单不可再次关闭或追加处理记录。"
         "关闭操作会写入独立的审计日志。"
)
@click.option("--batch-id", required=True, help="批次 ID")
@click.option("--ticket-id", required=True, help="处置单 ID")
@click.option("--operator", required=True, help="操作人姓名/工号")
@click.option("--reason", "close_reason", default="", help="关闭原因")
@click.pass_context
def incident_close(
    ctx: click.Context,
    batch_id: str,
    ticket_id: str,
    operator: str,
    close_reason: str,
):
    storage: Storage = ctx.obj["storage"]
    batch = storage.get_batch(batch_id)
    if not batch:
        err_console.print(f"批次不存在: {batch_id}")
        ctx.exit(ExitCode.BATCH_NOT_FOUND)

    ticket, error = close_incident(
        batch=batch,
        ticket_id=ticket_id,
        operator=operator,
        close_reason=close_reason,
    )
    _print_incident_closed(ticket, error)
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
