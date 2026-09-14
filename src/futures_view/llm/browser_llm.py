from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from ..constants import COMPANY_KEYS
from ..utils import load_yaml, save_json, shanghai_today

ATTACHMENT_FAILURE_MARKERS = (
    "未检测到您提到的附件", "没有收到任何上传的文件", "没有收到附件",
    "未收到附件", "重新上传该文件", "请重新上传", "附件内容直接粘贴",
)


def content_limit_failure(text: str) -> str | None:
    compact = re.sub(r"\s+", "", text)
    if "超出字数限制" in compact:
        match = re.search(r"元宝已阅读(\d+)%", compact)
        return f"元宝附件超出字数限制，仅读取{match.group(1)}%" if match else "元宝附件超出字数限制"
    match = re.search(r"元宝已阅读(\d+)%", compact)
    if match and int(match.group(1)) < 100:
        return f"元宝附件仅读取{match.group(1)}%"
    return None


def write_log(path: Path | None, message: str) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")


def emit(message: str, log_path: Path | None = None) -> None:
    print(message, flush=True)
    write_log(log_path, message)


def parse_json_response(text: str, target_date: str, company: str) -> dict:
    value = text.strip()
    value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.I)
    value = re.sub(r"\s*```$", "", value)
    decoder = json.JSONDecoder()
    parsed: list[dict] = []
    # Yuanbao occasionally omits the opening quote of a property name in long
    # JSON replies (most often `,confidence":`). Retry only this narrow, safe
    # repair; arbitrary malformed prose is still rejected.
    repaired = re.sub(
        r',(?=(?:company|sector|commodity|view|view_date|source_date|source_type|'
        r'source_name|source_title|source_url|evidence|confidence)":)',
        ',"', value,
    )
    for candidate in dict.fromkeys((value, repaired)):
        for index, char in enumerate(candidate):
            if char not in "{[":
                continue
            try:
                obj, _ = decoder.raw_decode(candidate[index:])
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(obj, list):
                obj = {"target_date": target_date, "company": company, "records": obj}
            if isinstance(obj, dict) and isinstance(obj.get("records"), list):
                obj.setdefault("target_date", target_date)
                obj.setdefault("company", company)
                for record in obj["records"]:
                    if isinstance(record, dict) and isinstance(record.get("source_url"), str):
                        record["source_url"] = re.sub(r"^httpshttps://", "https://", record["source_url"])
                parsed.append(obj)
    if parsed:
        exact = [obj for obj in parsed if obj.get("target_date") == target_date
                 and obj.get("company") == company]
        return (exact or parsed)[-1]
    raise ValueError("模型回复中没有合法的 JSON object/records 数组")


async def first_visible(page, selectors):
    for selector in selectors:
        matches = page.locator(selector)
        try:
            for index in range(await matches.count() - 1, -1, -1):
                locator = matches.nth(index)
                if await locator.is_visible():
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


def attachment_failure_response(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    return any(re.sub(r"\s+", "", marker) in compact for marker in ATTACHMENT_FAILURE_MARKERS)


def possible_model_response(text: str) -> bool:
    if attachment_failure_response(text) or content_limit_failure(text) is not None:
        return True
    try:
        parse_json_response(text, "", "")
        return True
    except ValueError:
        return False


async def attachment_is_visible(page, prompt_path: Path) -> bool:
    for locator in (
        page.get_by_text(prompt_path.name, exact=False).last,
        page.get_by_text(prompt_path.stem, exact=True).last,
    ):
        try:
            if await locator.count() and await locator.is_visible():
                return True
        except Exception:
            pass
    return False


async def upload_prompt_document(page, prompt_path: Path, llm: dict) -> None:
    absolute_path = prompt_path.resolve()
    attempts = int(llm.get("attachment_upload_attempts", 3))
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        file_inputs = page.locator('input[type="file"]')
        count_before = await file_inputs.count()
        add_button = await first_visible(page, llm.get("add_attachment_selectors", []))
        if not add_button:
            raise RuntimeError("找不到元宝的“添加”文件按钮")
        await add_button.click()
        await page.wait_for_timeout(500)
        file_inputs = page.locator('input[type="file"]')
        if await file_inputs.count() > count_before:
            await file_inputs.last.set_input_files(str(absolute_path))
        else:
            upload_option = await first_visible(page, llm.get("upload_file_option_selectors", []))
            if not upload_option:
                raise RuntimeError("“添加”菜单中找不到“本地文件/上传文件”选项")
            try:
                async with page.expect_file_chooser(timeout=5_000) as chooser_info:
                    await upload_option.click()
                chooser = await chooser_info.value
                await chooser.set_files(str(absolute_path))
            except PlaywrightTimeoutError as exc:
                raise RuntimeError("点击上传文件后没有出现文件选择控件") from exc
        try:
            filename = page.get_by_text(prompt_path.name, exact=False).last
            filename_stem = page.get_by_text(prompt_path.stem, exact=True).last
            timeout_ms = int(llm.get("attachment_ready_timeout_seconds", 60)) * 1000
            try:
                await filename.wait_for(state="visible", timeout=min(timeout_ms, 5_000))
            except PlaywrightTimeoutError:
                await filename_stem.wait_for(state="visible", timeout=timeout_ms)
            last_error = None
            break
        except PlaywrightTimeoutError as exc:
            last_error = exc
            print(f"[元宝] 附件未就绪，第 {attempt}/{attempts} 次上传失败：{prompt_path.name}", flush=True)
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(1500)
    if last_error is not None:
        raise RuntimeError(f"上传重试 {attempts} 次后仍未确认附件已就绪：{prompt_path.name}") from last_error
    await page.wait_for_timeout(int(llm.get("attachment_settle_seconds", 5)) * 1000)
    if not await attachment_is_visible(page, prompt_path):
        raise RuntimeError(f"附件等待完成后已从输入区消失：{prompt_path.name}")
    print(f"[元宝] 已上传 Prompt 文档：{prompt_path.name}")


async def prepare_prompt_delivery(
    page, prompt_path: Path, prompt_text: str, llm: dict, task_id: str,
    access_mode: str = "logged_in",
) -> tuple[str, bool]:
    mode = str(llm.get("prompt_delivery", "auto")).lower()
    threshold = int(llm.get("prompt_upload_threshold_chars", 6000))
    should_upload = (access_mode != "anonymous"
                     and (mode == "upload" or (mode == "auto" and len(prompt_text) > threshold)))
    if not should_upload:
        suffix = "（匿名模式不支持附件）" if access_mode == "anonymous" else ""
        print(f"[元宝] Prompt 共 {len(prompt_text)} 字，直接输入{suffix}")
        return f"【任务ID：{task_id}】\n{prompt_text}", False
    await upload_prompt_document(page, prompt_path, llm)
    return (
        f"【任务ID：{task_id}】请完整读取附件《{prompt_path.name}》，"
        "严格执行其中全部检索、分类、证据和输出规则。"
        "不要省略附件内容，不要解释过程；完成后只返回附件所要求的合法 JSON object。"
    ), True


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


def find_parseable_response(texts: list[str], target_date: str, company: str) -> tuple[str, dict] | None:
    """Return the longest DOM message that contains a records JSON object."""
    matches: list[tuple[str, dict]] = []
    for text in texts:
        try:
            matches.append((text, parse_json_response(text, target_date, company)))
        except ValueError:
            continue
    return max(matches, key=lambda item: len(item[0])) if matches else None


async def recover_failed_conversation(
    page, error_path: Path, selectors: list[str], target_date: str, company: str,
    prompt_sha256: str, diagnostic_path: Path,
) -> tuple[str, dict, str, str] | None:
    """Recover a completed Yuanbao reply after the previous watcher timed out."""
    if not error_path.exists():
        return None
    try:
        previous = json.loads(error_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    url = str(previous.get("page_url") or "")
    if (previous.get("prompt_sha256") != prompt_sha256
            or not re.search(r"/chat/[^/?#]+", url)):
        return None
    await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    await page.wait_for_timeout(2500)
    texts = await collect_message_texts(page, selectors)
    match = find_parseable_response(texts, target_date, company)
    if match:
        raw, result = match
        return raw, result, page.url, str(previous.get("task_id") or "")
    # Preserve bounded evidence for selector/debug work instead of only a screenshot.
    longest = sorted(texts, key=len, reverse=True)[:12]
    diagnostic_path.write_text(
        "\n\n===== DOM CANDIDATE =====\n\n".join(longest), encoding="utf-8"
    )
    return None


async def wait_for_response(
    page, selectors: list[str], before: set[str], timeout_seconds: int, submitted_prompt: str,
    stable_seconds: int, minimum_wait_seconds: int, generating_selectors: list[str],
    label: str, log_path: Path | None
) -> str:
    last = ""
    stable = 0
    for elapsed in range(1, timeout_seconds + 1):
        await page.wait_for_timeout(1000)
        if elapsed % 5 == 0:
            width = 24
            filled = min(width, int(width * elapsed / timeout_seconds))
            bar = "█" * filled + "░" * (width - filled)
            print(f"\r[元宝进度] {label} [{bar}] {elapsed:>3}/{timeout_seconds}s", end="", flush=True)
            try:
                page_text = await page.locator("body").inner_text()
                limit_error = content_limit_failure(page_text)
                if limit_error:
                    print()
                    raise RuntimeError(limit_error)
            except RuntimeError:
                raise
            except Exception:
                pass
        if elapsed % 30 == 0:
            write_log(log_path, f"等待回复：{label}，已等待 {elapsed}/{timeout_seconds} 秒")
        current = await collect_message_texts(page, selectors)
        prompt = submitted_prompt.strip()
        fresh = [
            text for text in current
            if text not in before
            and possible_model_response(text)
            and not (text.startswith(prompt) and len(text) <= len(prompt) + 30)
        ]
        if fresh:
            candidate = max(fresh, key=len)
            if candidate == last:
                stable += 1
            else:
                last, stable = candidate, 0
            # Candidate text is already restricted to parseable records JSON or an
            # explicit attachment/content-limit error. Yuanbao keeps a completed
            # “思考中” summary label visible, so that label cannot gate completion.
            if stable >= stable_seconds and elapsed >= minimum_wait_seconds:
                print()
                return last
    print()
    if last:
        return last
    raise TimeoutError("等待模型 JSON 回复超时")


async def wait_for_chat_route(page, timeout_seconds: int = 20) -> str:
    for _ in range(timeout_seconds * 2):
        if re.search(r"/chat/[^/?#]+", page.url):
            return page.url
        await page.wait_for_timeout(500)
    raise RuntimeError(f"发送后没有进入聊天页面，当前URL={page.url}")


async def wait_for_sent_task_marker(page, task_id: str, timeout_seconds: int = 20) -> None:
    marker = page.get_by_text(task_id, exact=False).last
    try:
        await marker.wait_for(state="visible", timeout=timeout_seconds * 1000)
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(f"点击发送后未在用户消息中找到任务编号 {task_id}，消息可能未提交") from exc


async def visible_login_dialog(page):
    for selector in (
        '.t-dialog:has-text("微信登录")',
        '.t-dialog__position:has-text("微信登录")',
        '[role="dialog"]:has-text("微信登录")',
    ):
        dialog = await first_visible(page, [selector])
        if dialog:
            return dialog
    return None


async def dismiss_login_dialog(page) -> bool:
    """Dismiss Yuanbao's delayed login modal without accepting or logging in."""
    dialog = await visible_login_dialog(page)
    if not dialog:
        return True
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(400)
    if not await visible_login_dialog(page):
        return True
    for selector in (
        '.t-dialog__close',
        '[aria-label="关闭"]',
        'button:has-text("关闭")',
        '[class*="close"]',
    ):
        close = await first_visible(dialog, [selector])
        if close:
            try:
                await close.click(force=True)
                await page.wait_for_timeout(500)
                if not await visible_login_dialog(page):
                    return True
            except Exception:
                continue
    # Tencent sometimes renders the X as an unlabelled SVG. Click the dialog's
    # upper-right close area as a final bounded fallback.
    box = await dialog.bounding_box()
    if box:
        await page.mouse.click(box["x"] + box["width"] - 28, box["y"] + 28)
        await page.wait_for_timeout(500)
    return not bool(await visible_login_dialog(page))


async def ensure_yuanbao_access(page, llm: dict, require_login: bool = False) -> str:
    """Use anonymous chat when available; pause for login only when required."""
    body_text = await page.locator("body").inner_text()
    if "未登录" not in body_text and "微信登录" not in body_text:
        return "logged_in"
    if not require_login and llm.get("allow_anonymous", True):
        dismissed = await dismiss_login_dialog(page)
        input_box = await first_visible(page, llm.get("input_selectors", []))
        if dismissed and input_box:
            try:
                if await input_box.is_editable():
                    print("[元宝] 未登录，已关闭登录弹窗并使用匿名对话", flush=True)
                    return "anonymous"
            except Exception:
                pass
    print("[元宝] 当前项目浏览器未登录。请在此窗口扫码，确认页面显示账号后回到终端按 Enter。", flush=True)
    await asyncio.to_thread(input)
    await page.wait_for_timeout(2500)
    body_text = await page.locator("body").inner_text()
    if "未登录" in body_text or "微信登录" in body_text:
        raise RuntimeError("扫码后页面仍显示未登录；请确认登录已完成后再按 Enter")
    return "logged_in"


async def run(args) -> None:
    cfg = load_yaml(args.config)
    llm = cfg["llm_web"]
    url = llm.get("url") or ""
    if not url:
        raise SystemExit("请先在 config.yaml 填 llm_web.url。")
    profile = Path(llm.get("profile_dir", "browser_profile/llm"))
    profile.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        try:
            context = await playwright.chromium.launch_persistent_context(
                str(profile), headless=False, locale="zh-CN", timezone_id="Asia/Shanghai",
                viewport={"width": 1440, "height": 960},
                args=["--disable-blink-features=AutomationControlled"],
            )
        except PlaywrightError as exc:
            raise SystemExit(
                "无法打开项目元宝浏览器：同一登录窗口可能仍在运行。请关闭该元宝窗口，"
                "确认之前的 run_all/run_company 已结束后再重试。"
            ) from exc
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_timeout(1800)
        access_mode = await ensure_yuanbao_access(page, llm, require_login=args.login_only)
        if args.login_only:
            print(f"[元宝] 登录状态已确认（{access_mode}），将保存项目浏览器登录资料。")
            await page.wait_for_timeout(3000)
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
            body_text = await page.locator("body").inner_text()
            print(f"[界面检查] 页面是否出现‘临时对话’={'是' if '临时对话' in body_text else '否'}")
            print("[界面检查] new_chat_selectors 实际命中：")
            for selector in llm.get("new_chat_selectors", []):
                matches = page.locator(selector)
                for match_index in range(await matches.count()):
                    match = matches.nth(match_index)
                    try:
                        if not await match.is_visible():
                            continue
                        print(" | ".join([
                            f"selector={selector}",
                            f"tag={await match.evaluate('el => el.tagName')}",
                            f"text={(await match.inner_text()).strip()[:100]}",
                            f"aria={await match.get_attribute('aria-label') or ''}",
                            f"title={await match.get_attribute('title') or ''}",
                            f"class={(await match.get_attribute('class') or '')[:180]}",
                            "clickable=" + (await match.evaluate("""el => {
                              const target = el.closest('button,a,[role="button"],[onclick]') || el.parentElement;
                              return target ? target.outerHTML.slice(0, 500) : '';
                            }""")).replace("\n", " "),
                        ]))
                    except Exception:
                        pass
            print("[界面检查] 对话相关控件：")
            controls = page.locator('button, [role="button"], a, [title], [aria-label]')
            for index in range(min(await controls.count(), 300)):
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
                    joined = " | ".join(part.replace("\n", " ")[:240] for part in parts)
                    if any(marker in joined for marker in ("对话", "会话", "临时", "chat", "新建")):
                        print(joined)
                except Exception:
                    pass
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
        normalized_path = Path(f"data/normalized/{stamp}/documents.json")
        normalized = json.loads(normalized_path.read_text(encoding="utf-8"))
        documents = normalized.get("documents", [])
        out_dir = Path(f"data/llm_output/{stamp}")
        out_dir.mkdir(parents=True, exist_ok=True)
        parts_dir = out_dir / "parts"
        parts_dir.mkdir(parents=True, exist_ok=True)
        log_path = out_dir / "run.log"
        emit(f"=== 元宝运行开始 {target_date} | Prompt任务数={len(entries)} ===", log_path)
        max_submissions = int(llm.get("max_submissions_per_run", 4))
        if len(entries) > max_submissions:
            raise RuntimeError(f"本次 Prompt 数 {len(entries)} 超过安全上限 {max_submissions}，已停止发送")
        submitted_parts: set[str] = set()
        aggregates: dict[str, dict] = {}
        for entry in entries:
            key = entry["key"]
            aggregates.setdefault(key, {
                "company": entry["company"], "crawl": entry.get("crawl", {}),
                "records": [], "parts": [], "errors": [], "empty_status": None,
            })

        message_selectors = list(dict.fromkeys([
            *llm.get("assistant_message_selectors", []),
            '[class*="agent-chat__conv"]', '[class*="message-content"]',
            '[class*="markdown"]', '[class*="hyc-component-reasoner"]',
        ]))
        for index, entry in enumerate(entries, start=1):
            key, company = entry["key"], entry["company"]
            crawl = entry.get("crawl", {})
            part_index = int(entry.get("part_index", 1))
            part_count = int(entry.get("part_count", 1))
            part_id = f"{key}_part{part_index:02d}"
            task_id = f"{stamp}-{part_id}-{datetime.now().strftime('%H%M%S')}"
            label = f"{company} {part_index}/{part_count}"
            aggregate = aggregates[key]
            prompt_path = Path(entry["prompt_file"])
            prompt_text = prompt_path.read_text(encoding="utf-8")
            prompt_sha256 = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
            part_result_path = parts_dir / f"{part_id}.json"
            if entry.get("deterministic_only"):
                # Local import avoids a module-level cycle: api_llm reuses this
                # module's response parser.
                from .api_llm import gtja_python_records
                records, rejected = gtja_python_records(documents, target_date)
                aggregate["records"].extend(records)
                aggregate["parts"].append({
                    "part_index": part_index, "status": "ok", "records": len(records),
                    "prompt_file": str(prompt_path), "prompt_sha256": prompt_sha256,
                    "deterministic_only": True, "rejected": len(rejected),
                })
                emit(f"[{index}/{len(entries)}] {label}：Python本地分类完成，"
                     f"{len(records)} 条，不调用元宝", log_path)
                continue
            if llm.get("resume_successful_parts", True) and part_result_path.exists():
                try:
                    existing = json.loads(part_result_path.read_text(encoding="utf-8"))
                    same_prompt = (existing.get("prompt_sha256") == prompt_sha256
                                   or (not existing.get("prompt_sha256")
                                       and part_result_path.stat().st_mtime >= prompt_path.stat().st_mtime))
                    if (existing.get("processing_status") == "ok"
                            and int(existing.get("part_count", 1)) == part_count and same_prompt):
                        existing_records = existing.get("records", [])
                        aggregate["records"].extend(existing_records)
                        aggregate["parts"].append({
                            "part_index": part_index, "status": "ok", "records": len(existing_records),
                            "prompt_file": str(prompt_path), "conversation_url": existing.get("conversation_url"),
                            "task_id": existing.get("task_id"), "prompt_sha256": prompt_sha256,
                            "result_file": str(part_result_path), "resumed": True,
                        })
                        emit(f"[{index}/{len(entries)}] {label}：断点续跑，跳过已成功分块（{len(existing_records)} 条）", log_path)
                        continue
                except Exception as exc:
                    emit(f"[{index}/{len(entries)}] {label}：旧分块不可复用，将重新处理 | {type(exc).__name__}: {exc}", log_path)
            error_path = parts_dir / f"error_{part_id}.json"
            if llm.get("resume_successful_parts", True) and error_path.exists():
                emit(f"[{index}/{len(entries)}] {label}：尝试从上次失败会话收取已完成回复", log_path)
                recovered = await recover_failed_conversation(
                    page, error_path, message_selectors, target_date, company,
                    prompt_sha256, out_dir / f"debug_{part_id}_dom.txt",
                )
                if recovered:
                    raw, result, conversation_url, recovered_task_id = recovered
                    part_result = {
                        **result, "processing_status": "ok", "crawl": crawl,
                        "part_index": part_index, "part_count": part_count,
                        "prompt_file": str(prompt_path), "conversation_url": conversation_url,
                        "task_id": recovered_task_id, "prompt_sha256": prompt_sha256,
                        "raw_response": raw, "recovered": True,
                    }
                    save_json(part_result_path, part_result)
                    aggregate["records"].extend(result["records"])
                    aggregate["parts"].append({
                        "part_index": part_index, "status": "ok", "records": len(result["records"]),
                        "prompt_file": str(prompt_path), "conversation_url": conversation_url,
                        "task_id": recovered_task_id, "prompt_sha256": prompt_sha256,
                        "result_file": str(part_result_path), "recovered": True,
                    })
                    error_path.unlink(missing_ok=True)
                    (out_dir / f"error_{part_id}.png").unlink(missing_ok=True)
                    (out_dir / f"debug_{part_id}_dom.txt").unlink(missing_ok=True)
                    emit(f"[{index}/{len(entries)}] {label}：已从旧会话恢复，未重复发送（{len(result['records'])} 条）", log_path)
                    continue
                if args.recover_only:
                    aggregate["errors"].append({
                        "target_date": target_date, "company": company,
                        "part_index": part_index, "part_count": part_count,
                        "prompt_file": str(prompt_path), "prompt_sha256": prompt_sha256,
                        "error": "旧会话中未找到可解析的完整 JSON；仅恢复模式未发送新消息",
                    })
                    aggregate["parts"].append({
                        "part_index": part_index, "status": "llm_error", "records": 0,
                        "prompt_file": str(prompt_path),
                        "error": "旧会话中未找到可解析的完整 JSON；仅恢复模式未发送新消息",
                    })
                    emit(f"[{index}/{len(entries)}] {label}：恢复失败；仅恢复模式已阻止重新发送", log_path)
                    continue
            if entry.get("source_count", 0) == 0 and not entry.get("fallback_search"):
                status = "crawl_error" if crawl.get("status") == "error" else "no_source_reports"
                aggregate["empty_status"] = status
                aggregate["parts"].append({"part_index": part_index, "status": status, "records": 0})
                emit(f"[{index}/{len(entries)}] {label}：{status}，不调用元宝", log_path)
                continue
            try:
                raw = ""
                emit(f"[{index}/{len(entries)}] {label}：开始", log_path)
                current_access_mode = await ensure_yuanbao_access(page, llm)
                if llm.get("new_chat_each_company", True):
                    button = await first_visible(page, llm.get("new_chat_selectors", []))
                    if button:
                        await button.click()
                        await page.wait_for_timeout(1200)
                    elif index > 1:
                        emit("[元宝] 未找到新建对话按钮，返回元宝首页创建独立对话", log_path)
                        await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
                        await page.wait_for_timeout(1200)
                current_access_mode = await ensure_yuanbao_access(page, llm)
                if "临时对话" in (await page.locator("body").inner_text()):
                    raise RuntimeError("检测到元宝临时对话模式；该模式不会保存历史记录，请先关闭临时对话")
                if current_access_mode == "anonymous":
                    emit("[元宝] 匿名模式不提供深度思考，使用默认回答", log_path)
                else:
                    await enable_deep_thinking(page, llm)
                before = set(await collect_message_texts(page, message_selectors))
                input_box = await first_visible(page, llm.get("input_selectors", []))
                if not input_box:
                    raise RuntimeError("找不到输入框；请确认已登录，或调整 llm_web.input_selectors")
                current_access_mode = await ensure_yuanbao_access(page, llm)
                if part_id in submitted_parts:
                    raise RuntimeError(f"检测到重复任务 {part_id}，已阻止再次发送")
                submitted_parts.add(part_id)
                emit(f"[{index}/{len(entries)}] 准备发送：{label} | Prompt={len(prompt_text)}字", log_path)
                submitted_text, attachment_expected = await prepare_prompt_delivery(
                    page, prompt_path, prompt_text, llm, task_id, current_access_mode)
                await input_box.fill(submitted_text)
                if attachment_expected and not await attachment_is_visible(page, prompt_path):
                    raise RuntimeError(f"填写短指令时附件被清除，已阻止发送：{prompt_path.name}")
                send = await first_visible(page, llm.get("send_selectors", []))
                if send:
                    await send.click()
                else:
                    await input_box.press("Enter")
                conversation_url = await wait_for_chat_route(page)
                await wait_for_sent_task_marker(page, task_id)
                emit(f"[{index}/{len(entries)}] 用户消息已确认 | 任务ID={task_id} | {conversation_url}", log_path)
                if attachment_expected:
                    await page.wait_for_timeout(1200)
                    if not await attachment_is_visible(page, prompt_path):
                        raise RuntimeError(f"发送后未在消息中确认附件：{prompt_path.name}")
                raw = await wait_for_response(
                    page, message_selectors, before,
                    int(llm.get("response_timeout_seconds", 300)), submitted_text,
                    int(llm.get("response_stable_seconds", 15)),
                    int(llm.get("minimum_response_wait_seconds", 15)),
                    llm.get("generating_selectors", []), label, log_path)
                if attachment_expected and attachment_failure_response(raw):
                    raise RuntimeError(f"元宝明确报告未收到附件：{prompt_path.name}")
                limit_error = content_limit_failure(raw)
                if limit_error:
                    raise RuntimeError(limit_error)
                result = parse_json_response(raw, target_date, company)
                part_result = {
                    **result, "processing_status": "ok", "crawl": crawl,
                    "part_index": part_index, "part_count": part_count,
                    "prompt_file": str(prompt_path), "conversation_url": conversation_url,
                    "task_id": task_id, "prompt_sha256": prompt_sha256,
                    "raw_response": raw,
                }
                save_json(parts_dir / f"{part_id}.json", part_result)
                aggregate["records"].extend(result["records"])
                aggregate["parts"].append({
                    "part_index": part_index, "status": "ok", "records": len(result["records"]),
                    "prompt_file": str(prompt_path), "conversation_url": conversation_url,
                    "task_id": task_id,
                    "prompt_sha256": prompt_sha256,
                    "result_file": str(parts_dir / f"{part_id}.json"),
                })
                (parts_dir / f"error_{part_id}.json").unlink(missing_ok=True)
                (out_dir / f"error_{part_id}.png").unlink(missing_ok=True)
                emit(f"[{index}/{len(entries)}] {label}：完成，{len(result['records'])} 条", log_path)
                if index < len(entries):
                    await page.wait_for_timeout(
                        int(llm.get("cooldown_between_companies_seconds", 3)) * 1000)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                diagnostic = {
                    "target_date": target_date, "company": company, "part_index": part_index,
                    "part_count": part_count, "prompt_file": entry.get("prompt_file"),
                    "page_url": page.url, "task_id": task_id,
                    "prompt_sha256": prompt_sha256, "error": error,
                }
                if raw:
                    diagnostic["raw_response"] = raw
                save_json(parts_dir / f"error_{part_id}.json", diagnostic)
                try:
                    await page.screenshot(path=str(out_dir / f"error_{part_id}.png"), full_page=True)
                except Exception:
                    pass
                aggregate["errors"].append(diagnostic)
                aggregate["parts"].append({
                    "part_index": part_index, "status": "llm_error", "records": 0,
                    "prompt_file": entry.get("prompt_file"), "error": error,
                })
                emit(f"[{index}/{len(entries)}] {label}：失败 | {error}", log_path)

        summary = []
        for key, aggregate in aggregates.items():
            unique_records = []
            seen = set()
            for record in aggregate["records"]:
                fingerprint = (
                    record.get("company"), record.get("sector"), record.get("commodity"),
                    record.get("view_date"), record.get("source_url"), record.get("evidence"),
                )
                if fingerprint not in seen:
                    seen.add(fingerprint)
                    unique_records.append(record)
            successful_parts = sum(part.get("status") == "ok" for part in aggregate["parts"])
            if aggregate["empty_status"]:
                status = aggregate["empty_status"]
            elif aggregate["errors"] and successful_parts:
                status = "partial_error"
            elif aggregate["errors"]:
                status = "llm_error"
            else:
                status = "ok"
            company_result = {
                "target_date": target_date, "company": aggregate["company"],
                "records": unique_records, "processing_status": status,
                "crawl": aggregate["crawl"], "parts": aggregate["parts"],
                "errors": aggregate["errors"],
            }
            company_path = out_dir / f"{key}.json"
            if not str(status).startswith("ok") and company_path.exists():
                try:
                    existing_company = json.loads(company_path.read_text(encoding="utf-8-sig"))
                except (json.JSONDecodeError, OSError):
                    existing_company = {}
                if (existing_company.get("target_date") == target_date
                        and str(existing_company.get("processing_status", "")).startswith("ok")):
                    existing_records = existing_company.get("records", [])
                    emit(
                        f"[公司汇总] {aggregate['company']}：本次状态={status}，"
                        f"保留已有成功结果（{len(existing_records)} 条），未降级覆盖",
                        log_path,
                    )
                    summary.append({
                        "key": key,
                        "status": existing_company.get("processing_status"),
                        "records": len(existing_records),
                        "parts_ok": sum(
                            str(part.get("status", "")).startswith("ok")
                            for part in existing_company.get("parts", [])
                        ),
                        "parts_total": len(existing_company.get("parts", [])),
                        "preserved_after_failed_run": True,
                    })
                    continue
            save_json(company_path, company_result)
            item = {"key": key, "status": status, "records": len(unique_records),
                    "parts_ok": successful_parts, "parts_total": len(aggregate["parts"])}
            if aggregate["errors"]:
                item["errors"] = [error["error"] for error in aggregate["errors"]]
            summary.append(item)
            emit(f"[公司汇总] {aggregate['company']}：status={status}，records={len(unique_records)}，"
                 f"parts={successful_parts}/{len(aggregate['parts'])}", log_path)
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
    parser.add_argument("--recover-only", action="store_true", help="只收取上次失败会话，不发送新 Prompt")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
