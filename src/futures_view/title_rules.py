from __future__ import annotations

import re


TITLE_VIEW_PATTERNS = {
    "中性": re.compile(r"震荡|盘整|整理|区间运行|区间波动|宽幅波动|观望|横盘"),
    "看多": re.compile(r"偏强|走强|转强|强势|重心上移|继续攀升|短期反弹|震荡上行|易涨难跌|支撑价格|仍有支撑|底部支撑|多头|逢低(?:做多|买入)|回调(?:做多|买入)"),
    "强烈看多": re.compile(r"强烈看多|显著上行|大幅上涨空间"),
    "看空": re.compile(r"偏弱|承压|仍有压力|短期转弱|继续回撤|中枢下移|(?:高位|冲高)回落|震荡下行|走弱|空头|逢高(?:做空|抛空)|抛空为主|下跌预期|情绪回落"),
    "强烈看空": re.compile(r"强烈看空|显著下行|大幅下跌空间"),
}


_BULLISH_TILT = re.compile(
    r"震荡(?:偏多|偏强|上行|回升|修复)|(?:偏多|偏强|上行|回升|修复)震荡|"
    r"偏多|偏强|走强|转强|趋强|重心上移|底部抬升|震荡重心上移|"
    r"仍有上涨空间|仍有提涨预期|延续强势|盘面回升|价格回升"
)
_BEARISH_TILT = re.compile(
    r"震荡(?:偏空|偏弱|下行|回落)|(?:偏空|偏弱|下行|回落)震荡|"
    r"偏空|偏弱|走弱|转弱|趋弱|重心下移|顶部承压|弱势难改|"
    r"承压(?:下跌|下落|回落)|盘面趋弱|延续调整|价格下调|"
    r"中长期.{0,20}下行预期|上方(?:持续)?承压|上方.{0,12}(?:压制|受压)"
)
_BULLISH_TRADE = re.compile(r"逢低(?:做多|买入)|低多|多单(?:机会|持有)|回调(?:做多|买入)")
_BEARISH_TRADE = re.compile(r"逢高(?:做空|抛空)|逢高空|高空|空配|空单持有")
_SUPPORTED_BUT_CAPPED = re.compile(
    r"(?:支撑|上涨空间|提涨预期).{0,100}(?:但|不过|然而|而).{0,100}"
    r"(?:"
    r"(?:(?:上方|上涨)(?:空间|高度)|上方).{0,30}(?:压制|受压|受限|限制|有限)"
    r"|(?:压制|限制).{0,30}(?:(?:上方|上涨)(?:空间|高度)|上方)"
    r")"
)
_PRESSURED_BUT_FLOORED = re.compile(
    r"(?:压力|承压).{0,100}(?:但|不过|然而|而).{0,100}"
    r"(?:下方空间|下跌空间|下方).{0,30}(?:支撑|受限|有限)"
)


def directional_view_override(text: str) -> str | None:
    """Return the ordinary directional grade when '震荡' has a clear tilt."""
    value = str(text or "")
    bull_trade = bool(_BULLISH_TRADE.search(value))
    bear_trade = bool(_BEARISH_TRADE.search(value))
    if bull_trade != bear_trade:
        return "看多" if bull_trade else "看空"
    if _SUPPORTED_BUT_CAPPED.search(value):
        return "看多"
    if _PRESSURED_BUT_FLOORED.search(value):
        return "看空"
    # Reuse the full ordinary-direction vocabulary as well as the more
    # context-sensitive tilt patterns.  This keeps phrases such as
    # “继续回撤/短期转弱” directional after neutral-overrides were added.
    bull = bool(_BULLISH_TILT.search(value) or TITLE_VIEW_PATTERNS["看多"].search(value))
    bear = bool(_BEARISH_TILT.search(value) or TITLE_VIEW_PATTERNS["看空"].search(value))
    if bull != bear:
        return "看多" if bull else "看空"
    return None


def infer_title_view(text: str) -> str | None:
    """Infer only when a title contains one of the explicit approved phrases."""
    for view in ("强烈看多", "强烈看空"):
        if TITLE_VIEW_PATTERNS[view].search(str(text or "")):
            return view
    directional = directional_view_override(text)
    if directional:
        return directional
    if TITLE_VIEW_PATTERNS["中性"].search(str(text or "")):
        return "中性"
    return None


def title_only_rejection(record: dict) -> str | None:
    """Reject title-only classifications unsupported by explicit direction text."""
    if record.get("source_type") != "官方网站标题":
        return None
    view = str(record.get("view", ""))
    evidence = str(record.get("evidence", ""))
    pattern = TITLE_VIEW_PATTERNS.get(view)
    if pattern is None:
        return "标题来源观点枚举无法校验"
    if not pattern.search(evidence):
        return f"标题证据没有明确支持观点“{view}”"
    return None
