from __future__ import annotations

import argparse
import subprocess
import sys

from .constants import COMPANY_KEYS
from .finalize import ensure_excel_output_available
from .utils import shanghai_today


def call(module: str, *arguments: str) -> None:
    command = [sys.executable, "-m", module, *[value for value in arguments if value]]
    print("[RUN]", " ".join(command), flush=True)
    completed = subprocess.run(command, check=False)
    if completed.returncode:
        raise SystemExit(f"[阶段失败] {module}，退出码={completed.returncode}；请查看上方最后一条具体错误。")


def main() -> None:
    parser = argparse.ArgumentParser(description="期货公司观点 Pipeline 总入口")
    parser.add_argument("--date", help="YYYY-MM-DD；默认上海时区当天")
    parser.add_argument("--company", choices=["all", *COMPANY_KEYS], default="all")
    parser.add_argument("--stage", choices=["all", "crawl", "prepare", "llm", "finalize"], default="all")
    parser.add_argument("--headed", action="store_true", help="显示官网采集浏览器，便于首轮调试")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--login-only", action="store_true", help="只打开元宝并保存首次手动登录状态")
    parser.add_argument("--llm-backend", choices=["browser", "api"], default="api",
                        help="LLM执行方式；默认腾讯TokenHub api，browser仅作备用")
    args = parser.parse_args()
    date = args.date or shanghai_today()
    stage_number = {"crawl": 1, "prepare": 2, "llm": 3, "finalize": 4}

    def announce(stage: str, detail: str) -> None:
        print(f"\n[总进度 {stage_number[stage]}/4] {detail}", flush=True)

    if args.login_only:
        call("futures_view.llm.browser_llm", "--login-only")
        return

    # A complete run can take time and consume paid API tokens.  Detect an
    # Excel lock before crawling or calling the model, not at the final step.
    if args.stage == "all":
        try:
            ensure_excel_output_available(date)
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc

    if args.stage in {"all", "crawl"} and args.company != "dzqh":
        announce("crawl", f"采集官网数据 | {date}")
        call("futures_view.pipeline", "--date", date, "--company", args.company,
             "--headed" if args.headed else "", "--debug" if args.debug else "")
    if args.stage in {"all", "prepare"}:
        announce("prepare", "标准化数据并生成 Prompt")
        call("futures_view.normalize", "--date", date)
        call("futures_view.build_prompts", "--date", date, "--company", args.company)
    if args.stage in {"all", "llm"}:
        if args.llm_backend == "api":
            announce("llm", "TokenHub API 分类与东证联网检索")
            call("futures_view.llm.api_llm", "--date", date, "--company", args.company)
        else:
            announce("llm", "元宝分类与东证检索")
            call("futures_view.llm.browser_llm", "--date", date, "--company", args.company)
    if args.stage in {"all", "finalize"}:
        announce("finalize", "校验、评分并生成 Excel")
        if args.company != "all":
            print("[提示] 单公司 finalize 仍会生成四家公司文件；未处理公司会明确标记 missing_llm_output。")
        call("futures_view.finalize", "--date", date)
        call("futures_view.inspect_run", "--date", date, "--strict")


if __name__ == "__main__":
    main()
