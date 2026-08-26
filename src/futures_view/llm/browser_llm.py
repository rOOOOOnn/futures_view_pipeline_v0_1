from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
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


async def element_is_active(locator) -> bool:
    try:
        class_name = (await locator.get_attribute("class") or "").lower()
        aria_pressed = (await locator.get_attribute("aria-pressed") or "").lower()
        data_state = (await locator.get_attribute("data-state") or "").lower()
        class_tokens = set(re.split(r"[\s_-]+", class_name))
        return (aria_pressed == "true" or data_state in {"active", "selected", "checked", "on"}
                or bool(class_tokens & {"active", "selected", "checked"}))
    except Exception:
        return False


async def enable_deep_thinking(page, llm: dict) -> None:
    if not llm.get("deep_thinking", False):
        return
    toggle = await first_visible(page, llm.get("deep_thinking_selectors", []))
    if toggle:
        if not await element_is_active(toggle):
            await toggle.click()
            await page.wait_for_timeout(800)
        print("[元宝] 已确认启用深度思考")
        return
    model_switch = await first_visible(page, llm.get("model_switch_selectors", []))
    if not model_switch:
        raise RuntimeError("找不到元宝模型切换按钮，无法启用深度思考")
    if "深度思考" not in (await model_switch.inner_text()):
        await model_switch.click()
        await page.wait_for_timeout(500)
        option = await first_visible(page, llm.get("deep_thinking_option_selectors", []))
        if not option:
            raise RuntimeError("模型菜单中找不到“深度思考”选项")
        await option.click()
        await page.wait_for_timeout(800)
    model_switch = await first_visible(page, llm.get("model_switch_selectors", []))
    if not model_switch or "深度思考" not in (await model_switch.inner_text()):
        raise RuntimeError("点击后未能确认元宝已切换到深度思考")
    print("[元宝] 已确认启用深度思考")


async def generation_is_active(page, selectors: list[str]) -> bool:
    return await first_visible(page, selectors) is not None


async def upload_prompt_document(page, prompt_path: Path, llm: dict) -> None:
    absolute_path = prompt_path.resolve()
    file_inputs = page.locator('input[type="file"]')
    if await file_inputs.count():
        await file_inputs.last.set_input_files(str(absolute_path))
    else:
        add_button = await first_visible(page, llm.get("add_attachment_selectors", []))
        if not add_button:
            raise RuntimeError("找不到元宝的“添加”文件按钮")
        await add_button.click()
        await page.wait_for_timeout(500)
        file_inputs = page.locator('input[type="file"]')
        if await file_inputs.count():
            await file_inputs.last.set_input_files(str(absolute_path))
        else:
            upload_option = await first_visible(page, llm.get("upload_file_option_selectors", []))
            if not upload_option:
                raise RuntimeError("“添加”菜单中找不到“上传文件”选项")
            try:
                async with page.expect_file_chooser(timeout=5_000) as chooser_info:
                    await upload_option.click()
                chooser = await chooser_info.value
                await chooser.set_files(str(absolute_path))
            except PlaywrightTimeoutError as exc:
                raise RuntimeError("点击上传文件后没有出现文件选择控件") from exc

    filename = page.get_by_text(prompt_path.name, exact=False).last
    filename_stem = page.get_by_text(prompt_path.stem, exact=True).last
    try:
        timeout_ms = int(llm.get("attachment_ready_timeout_seconds", 60)) * 1000
        try:
            await filename.wait_for(state="visible", timeout=min(timeout_ms, 5_000))
        except PlaywrightTimeoutError:
            await filename_stem.wait_for(state="visible", timeout=timeout_ms)
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(f"上传后未确认附件已就绪：{prompt_path.name}") from exc
    await page.wait_for_timeout(1500)
    print(f"[元宝] 已上传 Prompt 文档：{prompt_path.name}")


async def prepare_prompt_delivery(page, prompt_path: Path, prompt_text: str, llm: dict) -> str:
    mode = str(llm.get("prompt_delivery", "auto")).lower()
    threshold = int(llm.get("prompt_upload_threshold_chars", 6000))
    should_upload = mode == "upload" or (mode == "auto" and len(prompt_text) > threshold)
    if not should_upload:
        print(f"[元宝] Prompt 共 {len(prompt_text)} 字，直接输入")
        return prompt_text
    await upload_prompt_document(page, prompt_path, llm)
    return (
        f"请完整读取附件《{prompt_path.name}》，严格执行其中全部检索、分类、证据和输出规则。"
        "不要省略附件内容，不要解释过程；完成后只返回附件所要求的合法 JSON object。"
    )


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
    page, selectors: list[str], before: set[str], timeout_seconds: int, submitted_prompt: str,
    stable_seconds: int, minimum_wait_seconds: int, generating_selectors: list[str]
) -> str:
    last = ""
    stable = 0
    for elapsed in range(1, timeout_seconds + 1):
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
            if (stable >= stable_seconds and elapsed >= minimum_wait_seconds
                    and not await generation_is_active(page, generating_selectors)):
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
        if args.check_ui:
            input_box = await first_visible(page, llm.get("input_selectors", []))
            new_chat = await first_visible(page, llm.get("new_chat_selectors", []))
            deep_toggle = await first_visible(page, llm.get("deep_thinking_selectors", []))
            model_switch = await first_visible(page, llm.get("model_switch_selectors", []))
            if not deep_toggle and model_switch:
                await model_switch.click()
                await page.wait_for_timeout(800)
                deep_toggle = await first_visible(page, llm.get("deep_thinking_option_selectors", []))
            if deep_toggle and model_switch and "深度思考" not in (await model_switch.inner_text()):
                await deep_toggle.click()
                await page.wait_for_timeout(800)
                model_switch = await first_visible(page, llm.get("model_switch_selectors", []))
            deep_confirmed = bool(model_switch and "深度思考" in (await model_switch.inner_text()))
            print(f"[界面检查] 输入框={'正常' if input_box else '未找到'}；"
                  f"新建对话={'正常' if new_chat else '未找到'}；"
                  f"模型菜单={'正常' if model_switch else '未找到'}；"
                  f"深度思考={'已启用' if deep_confirmed else '未确认'}；未发送任何 Prompt")
            file_inputs = page.locator('input[type="file"]')
            print(f"[界面检查] 文件上传控件数量={await file_inputs.count()}")
            for index in range(await file_inputs.count()):
                file_input = file_inputs.nth(index)
                print("[文件控件] " + " | ".join([
                    await file_input.get_attribute("accept") or "",
                    await file_input.get_attribute("multiple") or "",
                    await file_input.get_attribute("class") or "",
                ]))
            if not deep_toggle:
                print("[界面检查] 当前可见按钮：")
                controls = page.locator('button, [role="button"]')
                for index in range(min(await controls.count(), 120)):
                    control = controls.nth(index)
                    try:
                        if not await control.is_visible():
                            continue
                        parts = [
                            (await control.inner_text()).strip(),
                            await control.get_attribute("aria-label") or "",
                            await control.get_attribute("title") or "",
                            await control.get_attribute("data-testid") or "",
                            await control.get_attribute("class") or "",
                        ]
                        print(" | ".join(part.replace("\n", " ")[:180] for part in parts))
                    except Exception:
                        pass
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
        max_submissions = int(llm.get("max_submissions_per_run", 4))
        if len(entries) > max_submissions:
            raise RuntimeError(f"本次 Prompt 数 {len(entries)} 超过安全上限 {max_submissions}，已停止发送")
        submitted_keys: set[str] = set()

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
                    elif index > 1:
                        print("[元宝] 未找到新建对话按钮，将在当前对话继续处理下一家公司")
                await enable_deep_thinking(page, llm)
                before = set(await collect_message_texts(page, message_selectors))
                input_box = await first_visible(page, llm.get("input_selectors", []))
                if not input_box:
                    raise RuntimeError("找不到输入框；请确认已登录，或调整 llm_web.input_selectors")
                prompt_path = Path(entry["prompt_file"])
                prompt_text = prompt_path.read_text(encoding="utf-8")
                if key in submitted_keys:
                    raise RuntimeError(f"检测到重复公司 {key}，已阻止再次发送")
                submitted_keys.add(key)
                print(f"[{index}/{len(entries)}] 准备发送：{company}（本公司仅发送一次）")
                submitted_text = await prepare_prompt_delivery(
                    page, prompt_path, prompt_text, llm)
                tag = await input_box.evaluate("element => element.tagName")
                if tag == "TEXTAREA":
                    await input_box.fill(submitted_text)
                else:
                    await input_box.click()
                    await page.keyboard.press("Control+A")
                    await page.keyboard.insert_text(submitted_text)
                send = await first_visible(page, llm.get("send_selectors", []))
                if send:
                    await send.click()
                else:
                    await input_box.press("Enter")
                raw = await wait_for_response(
                    page, message_selectors, before,
                    int(llm.get("response_timeout_seconds", 300)), submitted_text,
                    int(llm.get("response_stable_seconds", 15)),
                    int(llm.get("minimum_response_wait_seconds", 15)),
                    llm.get("generating_selectors", []))
                result = parse_json_response(raw, target_date, company)
                result.update({"processing_status": "ok", "crawl": crawl, "raw_response": raw})
                save_json(out_dir / f"{key}.json", result)
                summary.append({"key": key, "status": "ok", "records": len(result["records"])})
                print(f"[{index}/{len(entries)}] {company}: {len(result['records'])} 条")
                if index < len(entries):
                    await page.wait_for_timeout(
                        int(llm.get("cooldown_between_companies_seconds", 3)) * 1000)
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
        summary_path = out_dir / "run_summary.json"
        previous = {}
        if summary_path.exists():
            try:
                previous_obj = json.loads(summary_path.read_text(encoding="utf-8"))
                previous = {item.get("key"): item for item in previous_obj.get("companies", [])}
            except (json.JSONDecodeError, OSError):
                previous = {}
        previous.update({item.get("key"): item for item in summary})
        ordered_summary = [previous[key] for key in COMPANY_KEYS if key in previous]
        save_json(summary_path, {"target_date": target_date, "companies": ordered_summary})
        await context.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="通过持久登录的网页版 LLM 处理四家公司 Prompt")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--date")
    parser.add_argument("--prompts")
    parser.add_argument("--company", choices=["all", *COMPANY_KEYS], default="all")
    parser.add_argument("--login-only", action="store_true")
    parser.add_argument("--check-ui", action="store_true", help="只检查元宝控件和深度思考，不发送 Prompt")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
