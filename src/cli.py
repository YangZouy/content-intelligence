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
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from src.config_loader import get_config
from src.graph import run_pipeline

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

def _execute_publish(
    file_path: str,
    platforms: List[str],
    format_optimize_mode: Optional[str] = None,
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
            )

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
    )


if __name__ == "__main__":
    app()
