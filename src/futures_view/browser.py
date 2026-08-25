from contextlib import asynccontextmanager
from playwright.async_api import async_playwright

@asynccontextmanager
async def browser_context(headed: bool = False):
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=not headed, args=["--disable-blink-features=AutomationControlled"])
    context = await browser.new_context(
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        viewport={"width": 1440, "height": 1000},
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"),
        accept_downloads=True,
    )
    try:
        yield context
    finally:
        await context.close(); await browser.close(); await pw.stop()
