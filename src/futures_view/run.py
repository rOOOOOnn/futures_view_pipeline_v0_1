from __future__ import annotations

import argparse
import subprocess
import sys

from .constants import COMPANY_KEYS
from .utils import shanghai_today


def call(module: str, *arguments: str) -> None:
    command = [sys.executable, "-m", module, *[value for value in arguments if value]]
    print("[RUN]", " ".join(command))
    subprocess.run(command, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="期货公司观点 Pipeline 总入口")
    parser.add_argument("--date", help="YYYY-MM-DD；默认上海时区当天")
    parser.add_argument("--company", choices=["all", *COMPANY_KEYS], default="all")
    parser.add_argument("--stage", choices=["all", "crawl", "prepare", "llm", "finalize"], default="all")
    parser.add_argument("--headed", action="store_true", help="显示官网采集浏览器，便于首轮调试")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--login-only", action="store_true", help="只打开元宝并保存首次手动登录状态")
    args = parser.parse_args()
    date = args.date or shanghai_today()

    if args.login_only:
        call("futures_view.llm.browser_llm", "--login-only")
        return

    if args.stage in {"all", "crawl"} and args.company != "dzqh":
        call("futures_view.pipeline", "--date", date, "--company", args.company,
             "--headed" if args.headed else "", "--debug" if args.debug else "")
    if args.stage in {"all", "prepare"}:
        call("futures_view.normalize", "--date", date)
        call("futures_view.build_prompts", "--date", date, "--company", args.company)
    if args.stage in {"all", "llm"}:
        call("futures_view.llm.browser_llm", "--date", date, "--company", args.company)
    if args.stage in {"all", "finalize"}:
        if args.company != "all":
            print("[提示] 单公司 finalize 仍会生成四家公司文件；未处理公司会明确标记 missing_llm_output。")
        call("futures_view.finalize", "--date", date)


if __name__ == "__main__":
    main()
