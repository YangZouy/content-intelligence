"""
CLI 模块 —— 基于 Typer + Rich 的命令行发布入口。

提供单个命令：
- `publish` —— 完整流水线执行
- 用法：publish ./article.md [--platforms wechat] [-fo llm]

Typer：python的命令行框架，将普通python函数变成终端命令，根据函数自动解析命令行参数、生成--help、校验类型
Rich：终端美化输出库，用于彩色文字、表格、面板、进度/加载动画等
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import typer
import questionary
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from src.config_loader import get_config
from src.graph import (
    get_pending_approval,
    resume_pipeline,
    retry_pipeline_publish,
    run_pipeline,
)
from src.evaluation import run_evaluation, write_evaluation_report

# --- Typer app instance ---
# 创建整个CLI应用对象，后续所有的@app.command()
# 都会注册到它上面
app = typer.Typer(
    # CLI程序名
    name="content-dispatcher",
    # --help显示说明
    help="Personal Knowledge Content Intelligent Distribution System",
    # 如果用户没有输入子命令，不自动显示帮助
    no_args_is_help=False,
)

# --- Console for rich output ---
# Rich不用python内置的print() 输出，而是通过一个 Console 对象统一管理输出。
# Rich 的 Console()：
# 一个“增强版终端输出器”，能输出颜色、表格、边框、加载动画、Markdown 等。
console = Console()


# ---------------------------------------------------------------------------
# Display utilities (纯展示，不向用户提问)
# ---------------------------------------------------------------------------

def _display_banner() -> None:
    """Show application banner on startup."""
    banner = """[bold cyan]
╔══════════════════════════════════════════════╗
║                内容智能分发 v3.0              ║
╚══════════════════════════════════════════════╝
[/bold cyan]"""
    console.print(Panel(banner))


def _display_result_summary(final_state: Dict[str, Any], elapsed: float) -> None:
    run_log = final_state.get("run_log", {})

    # --- Overview table ---
    overview_table = Table(
        title="Pipeline Execution Summary",
        show_header=True,
        header_style="bold magenta",
        title_style="bold",
    )
    overview_table.add_column("Metric", style="cyan")
    overview_table.add_column("Value", style="green")

    # Basic info
    overview_table.add_row(
        "File", run_log.get("file_name", "N/A")
    )
    overview_table.add_row(
        "Source Type", run_log.get("source_type", "N/A")
    )
    overview_table.add_row(
        "Title", final_state.get("title", "N/A")
    )
    overview_table.add_row(
        "Total Duration", f"{elapsed:.2f}s"
    )
    overview_table.add_row(
        "Reading Time", final_state.get("reading_time", "N/A")
    )

    # Node durations
    node_durations = run_log.get("node_durations", {})
    if node_durations:
        duration_str = ", ".join(
            f"{k}={v:.1f}s" for k, v in node_durations.items()
        )
        overview_table.add_row("Node Timings", duration_str)

    console.print(overview_table)
    console.print()

    quality_status = final_state.get("quality_status")
    if quality_status == "failed":
        issues = final_state.get("quality_issues", [])
        issue_text = "\n".join(
            f"- {item.get('code', 'quality.unknown')}: {item.get('message', '')}"
            for item in issues
        )
        console.print(Panel(
            issue_text or "Quality gate failed.",
            title="Quality Gate Blocked Publishing",
            border_style="red",
        ))

    # --- Publish results table ---
    publish_results = final_state.get("publish_results", [])
    if publish_results:
        pub_table = Table(
            title="Publish Results",
            show_header=True,
            header_style="bold yellow",
        )
        pub_table.add_column("Platform", style="cyan")
        pub_table.add_column("Status", justify="center")
        pub_table.add_column("Attempts", justify="center")
        pub_table.add_column("URL / Error")

        for r in publish_results:
            success_str = (
                "[bold green]SUCCESS[/bold green]" if r.get("success")
                else "[bold red]FAILED[/bold red]"
            )
            if r.get("skipped"):
                success_str = "[cyan]REUSED[/cyan]"
            url_or_error = r.get("url") or r.get("error") or ""
            pub_table.add_row(
                r.get("platform", "?"),
                success_str,
                str(r.get("attempt", 0)),
                url_or_error[:80],
            )

        console.print(pub_table)

    # --- Token usage ---
    token_usage = run_log.get("token_usage", {})
    if token_usage:
        console.print(f"\n[bold]Token Usage:[/bold] {token_usage}")


def _display_approval_preview(state: Dict[str, Any]) -> None:
    request = state.get("approval_request", {})
    preview = request.get("preview", {})
    table = Table(title="Publish Approval", show_header=False)
    table.add_column("Field", style="cyan", width=18)
    table.add_column("Value")
    table.add_row("Run ID", state.get("run_id", ""))
    table.add_row("Title", preview.get("title", ""))
    table.add_row("Summary", preview.get("summary", ""))
    table.add_row("Tags", ", ".join(preview.get("tags", [])))
    table.add_row("Cover", preview.get("cover_url", "") or "(none)")
    table.add_row("Platforms", ", ".join(preview.get("requested_platforms", [])))
    console.print(table)
    console.print(
        f"[dim]This approval can be resumed later with: content-dispatcher resume "
        f"{state.get('run_id', '')}[/dim]"
    )


def _collect_approval_decision(state: Dict[str, Any]) -> Dict[str, Any]:
    _display_approval_preview(state)
    action = questionary.select(
        "Choose the next action:",
        choices=[
            questionary.Choice("Approve and publish", value="approve"),
            questionary.Choice("Modify and recheck", value="modify"),
            questionary.Choice("Reject and stop", value="reject"),
        ],
    ).ask()
    if action is None:
        raise KeyboardInterrupt
    if action != "modify":
        return {"action": action}

    preview = state.get("approval_request", {}).get("preview", {})
    title = questionary.text("Title:", default=preview.get("title", "")).ask()
    summary = questionary.text("Summary:", default=preview.get("summary", "")).ask()
    tags_text = questionary.text(
        "Tags (comma-separated):",
        default=", ".join(preview.get("tags", [])),
    ).ask()
    cover_url = questionary.text(
        "Cover URL:",
        default=preview.get("cover_url", ""),
    ).ask()
    platforms = questionary.checkbox(
        "Target platforms:",
        choices=[
            questionary.Choice(
                "Blog",
                value="blog",
                checked="blog" in preview.get("requested_platforms", []),
            ),
            questionary.Choice(
                "WeChat",
                value="wechat",
                checked="wechat" in preview.get("requested_platforms", []),
            ),
        ],
        validate=lambda selected: bool(selected) or "Select at least one platform",
    ).ask()
    if None in (title, summary, tags_text, cover_url, platforms):
        raise KeyboardInterrupt
    tags = [tag.strip() for tag in tags_text.split(",") if tag.strip()]
    return {
        "action": "modify",
        "changes": {
            "title": title.strip(),
            "summary": summary.strip(),
            "tags": tags,
            "cover_url": cover_url.strip(),
            "requested_platforms": platforms,
        },
    }


def _complete_approval(state: Dict[str, Any]) -> Dict[str, Any]:
    while state.get("approval_status") == "pending":
        decision = _collect_approval_decision(state)
        state = resume_pipeline(state["run_id"], decision)
    return state

def _execute_publish(
    file_path: str,
    platforms: List[str],
    format_optimize_mode: Optional[str] = None,
    article_slug: Optional[str] = None,
) -> Dict[str, Any]:
    
    start_time = time.time()

    try:
        with console.status(
            "[bold green]Processing content...[/bold green]", spinner="dots"
        ):
            # 开始执行
            final_state = run_pipeline(
                file_path=file_path,
                platforms=platforms,
                format_optimize_mode=format_optimize_mode,
                article_slug=article_slug,
            )

        final_state = _complete_approval(final_state)

        elapsed = time.time() - start_time
        # 打印overview信息（标题、来源等）和publish信息
        _display_result_summary(final_state, elapsed)
        return final_state

    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user.[/yellow]")
        raise typer.Exit(130)
    except Exception as e:
        elapsed = time.time() - start_time
        console.print(f"\n[bold red]Error after {elapsed:.1f}s:[/bold red] {e}")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# CLI Commands
# ---------------------------------------------------------------------------

@app.command()
def publish(
    # 必填位置参数：直接跟在 publish 后面，缺省则报错（无向导）
    file_path: str = typer.Argument(
        ...,
        help="Path to markdown file (e.g., ./article.md)",
    ),
    # 选项参数：必须带参数名使用 可简写 -p
    platforms: Optional[str] = typer.Option(
        None,
        "--platforms", "-p",
        help="Comma-separated target platforms: blog,wechat",
    ),
    # 覆盖 format_optimize 模式（rule | llm），不写则沿用 config.yaml
    format_optimize: Optional[str] = typer.Option(
        None,
        "--format-optimize", "-fo",
        help="Override format_optimize mode: rule | llm (default: from config.yaml)",
    ),
    slug: Optional[str] = typer.Option(
        None,
        "--slug",
        help="Stable article identity; use this if the source file may move",
    ),
) -> None:
    parsed_platforms: Optional[List[str]] = None
    if platforms:
        parsed_platforms = [p.strip() for p in platforms.split(",")]
        valid = {"blog", "wechat"}
        # 找出用户输入的平台中，哪些不在允许的列表里
        invalid = set(parsed_platforms) - valid
        if invalid:
            console.print(f"[red]Invalid platforms: {invalid}. Valid: {valid}[/red]")
            # 立刻停止该程序 1是命令行程序常用的失败退出码
            raise typer.Exit(1)

    # Validate format_optimize override
    if format_optimize is not None and format_optimize not in ("rule", "llm"):
        console.print("[red]--format-optimize must be 'rule' or 'llm'[/red]")
        raise typer.Exit(1)

    # File must exist
    if not Path(file_path).exists():
        console.print(f"[red]File not found: {file_path}[/red]")
        raise typer.Exit(1)

    # Show banner and publish directly (no wizard / no preview)
    _display_banner()
    target_platforms = parsed_platforms or list(get_config().get_platforms())
    _execute_publish(
        file_path,
        target_platforms,
        format_optimize_mode=format_optimize,
        article_slug=slug,
    )


@app.command()
def resume(
    run_id: str = typer.Argument(..., help="Run ID shown by a paused approval"),
) -> None:
    """Resume a workflow that is waiting for publish approval."""
    start_time = time.time()
    try:
        state = get_pending_approval(run_id)
        state = _complete_approval(state)
        _display_result_summary(state, time.time() - start_time)
    except KeyboardInterrupt:
        console.print("\n[yellow]Approval left pending.[/yellow]")
        raise typer.Exit(130)
    except Exception as exc:
        console.print(f"[bold red]Unable to resume:[/bold red] {exc}")
        raise typer.Exit(1)


@app.command("retry-publish")
def retry_publish(
    run_id: str = typer.Argument(..., help="Run ID with failed platform publishing"),
) -> None:
    """Retry failed publications while reusing successful platform results."""
    start_time = time.time()
    try:
        state = retry_pipeline_publish(run_id)
        _display_result_summary(state, time.time() - start_time)
    except Exception as exc:
        console.print(f"[bold red]Unable to retry publishing:[/bold red] {exc}")
        raise typer.Exit(1)


@app.command()
def evaluate(
    output: Optional[Path] = typer.Option(
        None,
        "--output", "-o",
        help="Path for the offline evaluation JSON report",
    ),
) -> None:
    """Run the fixed Markdown benchmark without external services."""
    try:
        report = run_evaluation()
        destination = output or Path("eval/reports/latest.json")
        write_evaluation_report(report, destination)
        console.print(Panel(
            "\n".join([
                f"Samples: {report['sample_count']}",
                f"Structure retention: {report['format_structure_retention_rate']:.1%}",
                f"Metadata structured: {report['metadata_structured_success_rate']:.1%}",
                f"Quality first pass: {report['quality_first_pass_rate']:.1%}",
                f"Expected outcome match: {report['quality_expectation_match_rate']:.1%}",
                f"Report: {destination}",
            ]),
            title="Offline Evaluation",
            border_style="cyan",
        ))
    except Exception as exc:
        console.print(f"[bold red]Evaluation failed:[/bold red] {exc}")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
