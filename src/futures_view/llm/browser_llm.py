from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path

from playwright.async_api import async_playwright

from ..constants import COMPANY_KEYS
from ..utils import load_yaml, save_json, shanghai_today


def parse_json_response(text: str, target_date: str, company: str) -> dict:
    value = text.strip()
    value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.I)
    value = re.sub(r"\s*```$", "", value)
    decoder = json.JSONDecoder()
    parsed: list[dict] = []
    for index, char in enumerate(value):
        if char not in "{[":
            continue
        try:
            obj, _ = decoder.raw_decode(value[index:])
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(obj, list):
            obj = {"target_date": target_date, "company": company, "records": obj}
        if isinstance(obj, dict) and isinstance(obj.get("records"), list):
            obj.setdefault("target_date", target_date)
            obj.setdefault("company", company)
            parsed.append(obj)
    if parsed:
        exact = [obj for obj in parsed if obj.get("target_date") == target_date
                 and obj.get("company") == company]
        return (exact or parsed)[-1]
    raise ValueError("模型回复中没有合法的 JSON object/records 数组")


async def first_visible(page, selectors):
    for selector in selectors:
        locator = page.locator(selector).last
        try:
            if await locator.count() and await locator.is_visible():
                return locator
        except Exception:
            pass
    return None


async def collect_message_texts(page, selectors: list[str]) -> list[str]:
    values: list[str] = []
    for selector in selectors:
        try:
            locator = page.locator(selector)
            for index in range(await locator.count()):
                text = (await locator.nth(index).inner_text()).strip()
                if text and text not in values:
                    values.append(text)
        except Exception:
            pass
    return values


async def wait_for_response(
    page, selectors: list[str], before: set[str], timeout_seconds: int, submitted_prompt: str
) -> str:
    last = ""
    stable = 0
    for _ in range(timeout_seconds):
        await page.wait_for_timeout(1000)
        current = await collect_message_texts(page, selectors)
        prompt = submitted_prompt.strip()
        fresh = [
            text for text in current
            if text not in before
            and ("records" in text or "target_date" in text)
            and not (text.startswith(prompt) and len(text) <= len(prompt) + 30)
        ]
        if fresh:
            candidate = max(fresh, key=len)
            if candidate == last:
                stable += 1
            else:
                last, stable = candidate, 0
            if stable >= 5:
                return last
    if last:
        return last
    raise TimeoutError("等待模型 JSON 回复超时")


async def run(args) -> None:
    cfg = load_yaml(args.config)
    llm = cfg["llm_web"]
    url = llm.get("url") or ""
    if not url:
        raise SystemExit("请先在 config.yaml 填 llm_web.url。")
    profile = Path(llm.get("profile_dir", "browser_profile/llm"))
    profile.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(profile), headless=False, locale="zh-CN", timezone_id="Asia/Shanghai",
            viewport={"width": 1440, "height": 960},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        if args.login_only:
            print("请在打开的元宝网页中手动扫码登录。确认已进入可输入的聊天页后，回到终端按 Enter。")
            await asyncio.to_thread(input)
            await context.close()
            return

        target_date = args.date or shanghai_today()
        stamp = target_date.replace("-", "")
        prompt_dir = Path(args.prompts or f"data/llm_input/{stamp}/prompts")
        manifest_obj = json.loads((prompt_dir / "manifest.json").read_text(encoding="utf-8"))
        entries = manifest_obj.get("prompts", [])
        if args.company != "all":
            entries = [entry for entry in entries if entry.get("key") == args.company]
        out_dir = Path(f"data/llm_output/{stamp}")
        out_dir.mkdir(parents=True, exist_ok=True)
        summary = []

        message_selectors = list(dict.fromkeys([
            *llm.get("assistant_message_selectors", []),
            '[class*="agent-chat__conv"]', '[class*="message-content"]',
            '[class*="markdown"]', '[class*="hyc-component-reasoner"]',
        ]))
        for index, entry in enumerate(entries, start=1):
            key, company = entry["key"], entry["company"]
            crawl = entry.get("crawl", {})
            if key != "dzqh" and entry.get("source_count", 0) == 0:
                status = "crawl_error" if crawl.get("status") == "error" else "no_source_reports"
                result = {"target_date": target_date, "company": company, "records": [],
                          "processing_status": status, "crawl": crawl}
                save_json(out_dir / f"{key}.json", result)
                summary.append({"key": key, "status": status, "records": 0})
                continue
            try:
                if llm.get("new_chat_each_company", True):
                    button = await first_visible(page, llm.get("new_chat_selectors", []))
                    if button:
                        await button.click()
                        await page.wait_for_timeout(1200)
                before = set(await collect_message_texts(page, message_selectors))
                input_box = await first_visible(page, llm.get("input_selectors", []))
                if not input_box:
                    raise RuntimeError("找不到输入框；请确认已登录，或调整 llm_web.input_selectors")
                prompt_text = Path(entry["prompt_file"]).read_text(encoding="utf-8")
                tag = await input_box.evaluate("element => element.tagName")
                if tag == "TEXTAREA":
                    await input_box.fill(prompt_text)
                else:
                    await input_box.click()
                    await page.keyboard.press("Control+A")
                    await page.keyboard.insert_text(prompt_text)
                send = await first_visible(page, llm.get("send_selectors", []))
                if send:
                    await send.click()
                else:
                    await input_box.press("Enter")
                raw = await wait_for_response(
                    page, message_selectors, before,
                    int(llm.get("response_timeout_seconds", 300)), prompt_text)
                result = parse_json_response(raw, target_date, company)
                result.update({"processing_status": "ok", "crawl": crawl, "raw_response": raw})
                save_json(out_dir / f"{key}.json", result)
                summary.append({"key": key, "status": "ok", "records": len(result["records"])})
                print(f"[{index}/{len(entries)}] {company}: {len(result['records'])} 条")
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                save_json(out_dir / f"{key}.json", {"target_date": target_date, "company": company,
                          "records": [], "processing_status": "llm_error", "crawl": crawl, "error": error})
                try:
                    await page.screenshot(path=str(out_dir / f"error_{key}.png"), full_page=True)
                except Exception:
                    pass
                summary.append({"key": key, "status": "llm_error", "error": error})
                print(f"[{index}/{len(entries)}] {company} 失败: {error}")
        save_json(out_dir / "run_summary.json", {"target_date": target_date, "companies": summary})
        await context.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="通过持久登录的网页版 LLM 处理四家公司 Prompt")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--date")
    parser.add_argument("--prompts")
    parser.add_argument("--company", choices=["all", *COMPANY_KEYS], default="all")
    parser.add_argument("--login-only", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
