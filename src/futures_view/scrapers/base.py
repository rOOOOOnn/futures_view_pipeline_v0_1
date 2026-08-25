from __future__ import annotations
import asyncio, json
from pathlib import Path
from typing import Any
from ..utils import save_json

class BaseScraper:
    company = ""
    key = ""
    start_url = ""

    def __init__(self, context, target_date: str, root: Path, debug: bool = True):
        self.context = context
        self.target_date = target_date
        self.root = root
        self.debug = debug
        self.network_json: list[dict[str, Any]] = []
        self.debug_dir = root / "debug" / self.key
        self.debug_dir.mkdir(parents=True, exist_ok=True)

    async def _capture_json(self, response):
        try:
            headers = await response.all_headers()
            if "json" not in headers.get("content-type", "").lower(): return
            body = await response.body()
            if len(body) > 8_000_000: return
            data = json.loads(body.decode("utf-8", errors="ignore"))
            self.network_json.append({"url": response.url, "status": response.status, "data": data})
        except Exception:
            pass

    async def new_page(self):
        page = await self.context.new_page()
        page.on("response", lambda r: asyncio.create_task(self._capture_json(r)))
        return page

    async def dump_debug(self, page, name: str):
        if not self.debug: return
        try: (self.debug_dir / f"{name}.html").write_text(await page.content(), encoding="utf-8")
        except Exception: pass
        try: await page.screenshot(path=str(self.debug_dir / f"{name}.png"), full_page=True)
        except Exception: pass
        if self.network_json: save_json(self.debug_dir / f"{name}_network.json", self.network_json)
