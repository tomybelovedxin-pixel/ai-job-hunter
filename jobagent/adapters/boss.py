"""BOSS 直聘适配器。

只做三件事：按关键词翻岗位列表、读岗位详情、点"立即沟通"发一条打招呼。
所有 DOM 选择器集中在上面的 SELECTORS 里，站点改版时只改这一处；
失配时不静默跳过，而是把页面快照存到 data/dump/ 供离线校准。
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .base import Adapter, AdapterError, BlockedError
from .. import guards

SEARCH_URL = "https://www.zhipin.com/web/geek/job?query={query}&city={city}&page={page}"
CITY_API = "https://www.zhipin.com/wapi/zpCommon/data/city.json"

# 兜底城市码；能联网时会用官方 city.json 覆盖
CITY_FALLBACK = {
    "全国": "100010000",
    "北京": "101010100",
    "上海": "101020100",
    "广州": "101280100",
    "深圳": "101280600",
    "杭州": "101210100",
    "成都": "101270100",
    "武汉": "101200100",
    "南京": "101190100",
    "苏州": "101190400",
    "西安": "101110100",
    "厦门": "101230200",
    "福州": "101230100",
    "泉州": "101230500",
}

SELECTORS: dict[str, list[str]] = {
    # 2026-10-12 晚按真实页面校准过：外层的 .job-card-wrap 里才是 .job-card-box，
    # 公司名在 .boss-name，地点在 .company-location（不是 .company-name）
    "card": [".job-card-wrap .job-card-box", ".job-card-box", "[class*='job-card']"],
    "title": ["a.job-name", ".job-name", ".job-title", "[class*='job-name']"],
    "company": [".boss-name", ".company-name", "[class*='company-name']"],
    "salary": [".salary", "[class*='salary']"],
    "city": [".company-location", ".job-area", "[class*='job-area']"],
    "tags": [".tag-list li", "[class*='tag-list'] li"],
    "hr_name": [".info-public .name", "[class*='info-public'] .name", ".info-public"],
    "hr_active": [".info-public em", "[class*='info-public'] em"],
    "detail_jd": [
        ".job-detail-section .desc",
        ".job-detail-section .job-sec-text",
        ".job-sec-text",
        ".job-detail-section",
        "[class*='job-detail'] .text",
    ],
    "start_chat": [
        "a.btn-startchat",
        ".btn-startchat",
        "[class*='startchat']",
        "button[class*='start-chat']",
    ],
    "chat_input": [
        "#chat-input",
        ".chat-input[contenteditable='true']",
        "textarea.chat-input",
        "div.chat-input",
        "[contenteditable='true']",
    ],
    "chat_send": [
        ".btn-send",
        ".btn-send",
        "button[class*='send']",
        ".send-message",
        "[class*='send-message']",
    ],
    # 聊天页左侧会话列表（2026-10-13 实测：li 在 .user-list-content 里，名字在 .name-box）
    "chat_rows": [".user-list-content li", ".user-list li", ".chat-user"],
    "chat_row_name": [".name-box", ".title-box", ".name"],
}

LOGIN_MARKERS = [
    "APP扫码登录", "扫码登录", "验证码登录", "登录/注册", "请先登录", "立即登录",
    # 聊天/沟通动作会单独弹登录框，文案是这两种
    "短信登录", "短信验证码", "使用 BOSS直聘 APP 扫码登录",
]
RISK_MARKERS = [
    "安全验证", "访问过于频繁", "操作太频繁", "访问异常",
    "拖动滑块", "滑块验证", "请完成验证", "人机验证",
]


def _js_list(name: str) -> str:
    return json.dumps(SELECTORS[name], ensure_ascii=False)


SCAN_JS = f"""
(() => {{
  const pick = (root, sels) => {{
    for (const s of sels) {{
      const el = root.querySelector(s);
      if (el && (el.textContent || '').trim()) return el.textContent.trim();
    }}
    return '';
  }};
  // 按优先级取第一个真正有结果的选择器，绝不取并集：
  // 宽泛的 [class*='job-card'] 会把卡片内部的元素也算成卡片。
  let cards = [];
  for (const s of {_js_list('card')}) {{
    const found = document.querySelectorAll(s);
    if (found.length) {{ cards = Array.from(found); break; }}
  }}
  const out = [];
  for (const card of cards) {{
    // 只认详情页链接。列表页里还混着公司卡片（/gongsi/…），
    // 上一版把它们也当岗位抓进来，库里多了 118 条空标题。
    const link = card.querySelector('a[href*="/job_detail/"]');
    if (!link) continue;
    const href = link ? link.href : '';
    const m = href.match(/job_detail\\/([^.?/]+)/);
    if (!m) continue;
    out.push({{
      external_id: m[1],
      url: href,
      title: pick(card, {_js_list('title')}),
      company: pick(card, {_js_list('company')}),
      salary_text: pick(card, {_js_list('salary')}),
      city: pick(card, {_js_list('city')}),
      tags: Array.from(card.querySelectorAll({_js_list('tags')}.join(',')))
        .map(e => (e.textContent || '').trim()).filter(Boolean),
      hr_name: pick(card, {_js_list('hr_name')}),
      hr_active: pick(card, {_js_list('hr_active')}),
      raw_text: (card.innerText || '').slice(0, 1500)
    }});
  }}
  return JSON.stringify(out);
}})()
"""

DETAIL_JS = f"""
(() => {{
  const pick = (sels) => {{
    for (const s of sels) {{
      const el = document.querySelector(s);
      if (el && (el.innerText || '').trim()) return el.innerText.trim();
    }}
    return '';
  }};
  return JSON.stringify({{
    title: pick(['.job-name', '.name h1', 'h1']),
    salary_text: pick({_js_list('salary')}),
    company: pick(['.sider-company .company-info a[title]', '.sider-company .company-info a',
                   '.sider-company .company-name', ...{_js_list('company')}]),
    meta: pick(['.job-primary .text', '.job-info', '[class*="job-info"]']),
    description: pick({_js_list('detail_jd')}),
    hr_name: pick(['.job-boss-info h2.name', ...{_js_list('hr_name')}]),
    hr_title: pick(['.boss-info-attr', '[class*="boss-info"]']),
    body_text: (document.body.innerText || '').slice(0, 3000)
  }});
}})()
"""

# 当前打开会话的消息文本：我方/对方分开放，用于"发送前先核对"和"读取新回复"
CONVERSATION_JS = """
(() => {
  const pane = document.querySelector('.chat-conversation, [class*="conversation"]');
  if (!pane) return JSON.stringify({pane: false, mine: [], theirs: []});
  const mine = [], theirs = [];
  for (const item of pane.querySelectorAll('li.message-item')) {
    const node = item.querySelector('.message-content') || item;
    const clean = ((node.innerText || '').trim());
    if (!clean) continue;
    if (item.className.includes('item-friend')) theirs.push(clean);
    else if (item.className.includes('item-myself')) mine.push(clean);
  }
  const job = (pane.querySelector('.left-content') || {}).innerText || '';
  const name = (pane.querySelector('.name-text') || {}).innerText || '';
  const company = (pane.querySelector('.user-info') || {}).innerText || '';
  return JSON.stringify({
    pane: true,
    name: name.trim(),
    company: company.replace(/\\n/g, ' ').trim().slice(0, 80),
    job: job.replace(/\\n/g, ' ').trim().slice(0, 80),
    input_len: (() => {
      const el = document.querySelector('#chat-input, .chat-input[contenteditable="true"], textarea.chat-input');
      return el ? ((el.innerText || el.value || '').trim().length) : -1;
    })(),
    mine: mine, theirs: theirs
  });
})()
"""

# 简历附件请求识别 JS：严格区分结构化卡片与普通聊天正文，杜绝误触“拒绝”按钮
RESUME_REQUEST_JS = """
(() => {
  const pane = document.querySelector('.chat-conversation, [class*="conversation"]');
  if (!pane) return JSON.stringify({ok: false, message: "未找到会话消息面板"});

  const items = Array.from(pane.querySelectorAll('li.message-item, .chat-message, [class*="message-item"]'));
  const cards = [];

  for (let i = 0; i < items.length; i++) {
    const item = items[i];
    const text = (item.innerText || '').trim();
    const isRequestCard = (
      text.includes("请求获取您的附件简历") ||
      text.includes("希望获取您的完整简历") ||
      text.includes("发起了简历请求") ||
      text.includes("请求发送简历") ||
      text.includes("求简历") ||
      text.includes("请求互换简历") ||
      text.includes("想要您的附件简历")
    );
    if (!isRequestCard) continue;

    const isAlreadyAccepted = (
      text.includes("已同意") ||
      text.includes("已发送简历") ||
      text.includes("已投递") ||
      text.includes("您已同意发送") ||
      text.includes("已将附件简历发送给对方")
    );

    const buttons = Array.from(item.querySelectorAll('button, a.btn, [class*="btn"], .confirm-btn, .btn-sure'));
    let targetButtonSelector = null;
    let targetButtonText = "";
    let rejectFound = false;

    for (let j = 0; j < buttons.length; j++) {
      const b = buttons[j];
      const bText = (b.innerText || b.textContent || '').trim();
      if (bText.includes("拒绝") || bText.includes("不合适") || bText.includes("取消") || bText.includes("关闭")) {
        rejectFound = true;
        continue;
      }
      if (bText.includes("同意") || bText.includes("发送附件") || bText.includes("立即发送") || bText === "发送") {
        const attrVal = "agree-" + i + "-" + j;
        b.setAttribute("data-jobagent-action", attrVal);
        targetButtonSelector = '[data-jobagent-action="' + attrVal + '"]';
        targetButtonText = bText;
      }
    }

    cards.push({
      index: i,
      snippet: text.slice(0, 150),
      is_already_accepted: isAlreadyAccepted,
      has_agree_button: !!targetButtonSelector,
      agree_button_text: targetButtonText,
      reject_button_present: rejectFound,
      button_selector: targetButtonSelector
    });
  }

  return JSON.stringify({
    ok: true,
    total_cards: cards.length,
    cards: cards
  });
})()
"""


def compute_card_fingerprint(company: str, hr_name: str, snippet: str) -> str:
    """生成稳定唯一的简历请求卡片指纹，结合公司、HR与卡片文本。"""
    norm_c = (company or "").strip().lower()
    norm_h = (hr_name or "").strip().lower()
    norm_s = "".join((snippet or "").split())
    raw = f"{norm_c}::{norm_h}::{norm_s}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


class BossAdapter(Adapter):
    name = "boss"
    home = "https://www.zhipin.com/web/geek/job"

    # ---------- 城市码 ----------

    @staticmethod
    def _load_city_map() -> dict[str, str]:
        mapping = dict(CITY_FALLBACK)
        request = urllib.request.Request(
            CITY_API,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
                "Referer": "https://www.zhipin.com/web/geek/job",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            for province in payload.get("zpData", {}).get("cityList", []):
                mapping[province.get("name", "")] = province.get("code", "")
                for city in province.get("subLevelModelList", []) or []:
                    if city.get("name"):
                        mapping[city["name"]] = str(city.get("code", ""))
        except Exception:
            pass
        return mapping

    def resolve_city(self, city: str | None) -> str:
        if not city:
            return CITY_FALLBACK["全国"]
        city = city.replace("市", "").strip()
        mapping = self._load_city_map()
        code = mapping.get(city)
        if code:
            self.log(f"城市 {city} -> {code}")
            return code
        self.log(f"没查到 {city} 的城市码，回退到全国（可用 --city-code 直接指定）")
        return CITY_FALLBACK["全国"]

    # ---------- 风控 ----------

    def check_block(self) -> str | None:
        try:
            body = self.session.evaluate(
                "document.body ? document.body.innerText.slice(0, 2000) : ''",
                await_promise=False,
            ) or ""
            url = self.session.url() or ""
        except Exception:
            return None
        for marker in RISK_MARKERS:
            if marker in body:
                return f"平台风控：{marker}（先手工过掉验证，再重跑）"
        if "/web/user/" in url:
            return "需要登录：页面停在登录页，请在这个调试窗口里手工登录一次"
        for marker in LOGIN_MARKERS:
            if marker in body:
                return "需要登录：页面停在登录页，请在这个调试窗口里手工登录一次"
        return None

    # ---------- 抓岗位列表 ----------

    def scan(self, query: str, *, city: str | None = None, pages: int = 1,
             limit: int | None = None, city_code: str | None = None,
             **_: Any) -> list[dict[str, Any]]:
        code = city_code or self.resolve_city(city)
        collected: list[dict[str, Any]] = []
        seen: set[str] = set()

        for page in range(1, pages + 1):
            url = SEARCH_URL.format(
                query=urllib.parse.quote(query), city=code, page=page
            )
            self.log(f"第 {page} 页: {url}")
            self.session.navigate(url)
            self.session.wait_ready(timeout=25)

            appeared = self.session.wait_for(
                f"document.querySelectorAll({_js_list('card')}.join(',')).length > 0",
                timeout=25,
            )
            if not appeared:
                block = self.check_block()
                snapshot = self.dump_html(f"scan-p{page}")
                detail = f"，页面快照: {snapshot}" if snapshot else ""
                if block:
                    raise BlockedError(f"被拦住了：{block}{detail}")
                raise AdapterError(
                    f"第 {page} 页没等到岗位卡片（选择器可能过期）{detail}"
                )

            raw = self.session.evaluate(SCAN_JS)
            cards = json.loads(raw or "[]")
            fresh = 0
            for card in cards:
                external_id = str(card.get("external_id") or "").strip()
                if not external_id or external_id in seen:
                    continue
                seen.add(external_id)
                card["platform"] = self.name
                collected.append(card)
                fresh += 1
            self.log(f"第 {page} 页取到 {fresh} 条（累计 {len(collected)}）")

            if limit and len(collected) >= limit:
                collected = collected[:limit]
                break
            time.sleep(1.5)  # 别把平台当敌人

        return collected

    # ---------- 读详情 ----------

    def fetch_detail(self, job: dict[str, Any]) -> dict[str, Any]:
        url = job.get("url")
        if not url:
            raise AdapterError("这条岗位没有 url，抓不到详情")
        self._open(url)

        block = self.check_block()
        if block:
            snapshot = self.dump_html("detail")
            raise BlockedError(f"详情页被拦住：{block}，快照: {snapshot}")

        # 岗位下线时 BOSS 返回"您访问的页面不存在"的空壳页；这种要跟"选择器过期"分开，
        # 否则每次都会把已下线的岗位重新抓一遍（2026-10-16）
        title = str(self.session.evaluate("document.title", await_promise=False) or "")
        if "页面不存在" in title or "不存在" in title:
            raise AdapterError(f"JOB_GONE: 岗位页面不存在（已下线）｜{title[:40]}")

        raw = self.session.evaluate(DETAIL_JS)
        detail = json.loads(raw or "{}")
        if not detail.get("description"):
            snapshot = self.dump_html("detail-nojd")
            raise AdapterError(f"详情页没抓到 JD 正文，快照: {snapshot}")
        detail["platform"] = self.name
        detail["external_id"] = job.get("external_id")
        detail["url"] = url
        return detail

    def _open(self, url: str, *, attempts: int = 2) -> None:
        """打开页面并等它就绪。慢页面重试一次，别让单条卡住整批。"""
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                self.session.navigate(url)
                self.session.wait_ready(timeout=30)
                self.session.wait_for("!!document.body", timeout=15)
                time.sleep(0.8)
                return
            except Exception as exc:  # 超时或脚本错误都重试一次
                last_error = exc
                self.log(f"打开页面失败（第 {attempt + 1} 次）：{type(exc).__name__}: {exc}")
                time.sleep(2.0)
        raise AdapterError(f"页面打不开：{url}（{type(last_error).__name__}: {last_error}）")

    # ---------- 发打招呼 ----------

    CHAT_URL = "https://www.zhipin.com/web/geek/chat"

    def click_expr(self, find_expr: str, *, timeout: float = 5.0) -> dict[str, Any]:
        """执行 JavaScript 元素表达式并点击该元素（mousedown / mouseup / click 三连冒泡派发）。

        参数契约：
        find_expr 必须是一段返回 DOM Element（或 null）的 JS 表达式代码。
        严禁传入 CSS 选择器字符串（CSS 选择器请使用 click_selector）。

        返回统一结构：
        {"ok": bool, "clicked": bool, "error": str | None}
        """
        js = (
            "(() => {"
            "  try {"
            "    const el = (" + find_expr + ");"
            "    if (!el) return JSON.stringify({ok: false, clicked: false, error: '未找到目标元素 (null)'});"
            "    if (!(el instanceof Element)) {"
            "      return JSON.stringify({ok: false, clicked: false, error: '表达式返回结果不是 DOM Element (类型: ' + typeof el + ')'});"
            "    }"
            "    for (const t of ['mousedown', 'mouseup', 'click']) {"
            "      el.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window}));"
            "    }"
            "    return JSON.stringify({ok: true, clicked: true, error: null});"
            "  } catch (err) {"
            "    return JSON.stringify({ok: false, clicked: false, error: 'JS表达式执行异常: ' + (err.message || String(err))});"
            "  }"
            "})()"
        )
        try:
            raw = self.session.evaluate(js, await_promise=False, timeout=timeout)
            if isinstance(raw, dict):
                return raw
            if isinstance(raw, str) and raw.startswith("{"):
                return json.loads(raw)
            return {"ok": bool(raw), "clicked": bool(raw), "error": None if raw else "未获得有效执行响应"}
        except Exception as exc:
            return {"ok": False, "clicked": False, "error": f"CDP 执行失败: {exc}"}

    def click_selector(self, selector: str, *, timeout: float = 5.0) -> dict[str, Any]:
        """使用 CSS 选择器定位元素并点击该元素（mousedown / mouseup / click 三连冒泡派发）。

        参数契约：
        selector 必须是标准的 CSS 选择器字符串。
        严禁传入 JS 表达式代码。

        返回统一结构：
        {"ok": bool, "clicked": bool, "error": str | None}
        """
        if not isinstance(selector, str) or not selector.strip():
            return {"ok": False, "clicked": False, "error": "CSS 选择器不能为空"}
        sel_json = json.dumps(selector.strip(), ensure_ascii=False)
        js = (
            "(() => {"
            "  try {"
            f"    const el = document.querySelector({sel_json});"
            "    if (!el) return JSON.stringify({ok: false, clicked: false, error: '未找到匹配选择器的元素: ' + " + sel_json + "});"
            "    if (!(el instanceof Element)) {"
            "      return JSON.stringify({ok: false, clicked: false, error: '选择器结果不是有效 DOM Element'});"
            "    }"
            "    for (const t of ['mousedown', 'mouseup', 'click']) {"
            "      el.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window}));"
            "    }"
            "    return JSON.stringify({ok: true, clicked: true, error: null});"
            "  } catch (err) {"
            "    return JSON.stringify({ok: false, clicked: false, error: 'CSS选择器语法无效或DOM查询异常: ' + (err.message || String(err))});"
            "  }"
            "})()"
        )
        try:
            raw = self.session.evaluate(js, await_promise=False, timeout=timeout)
            if isinstance(raw, dict):
                return raw
            if isinstance(raw, str) and raw.startswith("{"):
                return json.loads(raw)
            return {"ok": bool(raw), "clicked": bool(raw), "error": None if raw else "未获得有效执行响应"}
        except Exception as exc:
            return {"ok": False, "clicked": False, "error": f"CDP 执行失败: {exc}"}

    @staticmethod
    def _rows_expr() -> str:
        """返回会话列表行的 JS 表达式（数组）。"""
        selectors = _js_list("chat_rows")
        return (
            "(() => { let rows = [];"
            f" for (const s of {selectors}) {{"
            "   const found = document.querySelectorAll(s);"
            "   if (found.length) { rows = Array.from(found); break; }"
            " } return rows; })()"
        )

    @staticmethod
    def _company_keys(company: str) -> list[str]:
        """公司名的匹配候选：原名、去掉省略号的前缀、前 4 个字（BOSS 会截断名字）。

        注意长度下限是 2 不是 3：中文公司名常常只有两个字（"凌山""华琦"），
        之前用 3 过滤会把它们全丢掉，导致匹配候选为空、退回"发给列表第一条"——
        2026-10-13 因此发错过两条消息。
        """
        raw = (company or "").strip()
        keys = [raw, raw.rstrip(".．。"), raw[:4]]
        return [k for k in keys if k and len(k) >= 2]

    def current_conversation_text(self) -> str:
        """当前打开会话的面板文本（没打开会话时是空串）。"""
        return str(self.session.evaluate(
            "(() => { const el = document.querySelector("
            "'.chat-conversation, [class*=\"conversation\"]');"
            " return el ? (el.innerText || '').trim().slice(0, 300) : ''; })()",
            await_promise=False,
        ) or "")

    def conversation_history(self) -> dict[str, Any]:
        """当前打开会话的消息，我方/对方分开返回。

        用途有两个，都不需要截图：
        - 发送前核对：这条话术是不是已经发过了（不重复发）；
        - 读取回复：对方最后一条说了什么（只处理新增信息）。
        没打开会话时 mine/theirs 都是空列表，调用方要按"读不到"处理，不能当成"没回复"。
        """
        raw = self.session.evaluate(CONVERSATION_JS, await_promise=False)
        if not isinstance(raw, str):
            raw = ""
        try:
            data = json.loads(raw or "{}")
        except (json.JSONDecodeError, TypeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("mine", [])
        data.setdefault("theirs", [])
        data.setdefault("input_len", -1)
        data.setdefault("pane", False)
        return data

    def open_conversation(self, company: str = "", *, timeout: float = 20.0,
                          attempts: int = 3) -> str:
        """在聊天页选中目标会话，返回选中的会话标题（空串表示没选中）。

        实测结论：BOSS 的聊天页在没选中会话时**根本没有输入框**，
        所以"点完立即沟通就找输入框"一定失败。必须先在这里点选会话行，
        而且要用真实的 mousedown/mouseup/click 三连，`element.click()` 不触发 Vue 选中。

        点击沟通按钮后页面会整页重载，会话列表要等 XHR 回来才有；所以这里
        每次都显式重进聊天页并重试，不然会撞在"加载中，请稍候"那一刻。
        """
        rows_expr = self._rows_expr()
        for attempt in range(1, attempts + 1):
            try:
                self.session.navigate(self.CHAT_URL)
                self.session.wait_ready(timeout=30)
            except Exception as exc:  # 重载途中读页面会抛超时，重试即可
                self.log(f"第 {attempt} 次进聊天页失败：{type(exc).__name__}: {exc}")
                time.sleep(2.0)
                continue

            # 注意：列表会先渲染出一个占位行（实测 +2s 时只有 1 行，+6s 才有 40+ 行）。
            # 用 "> 0" 判断会在占位阶段就通过，然后在那一行里找不到公司名，
            # 三个 attempts 全撞在同一个竞态上——2026-10-13 的批量失败就是这个原因。
            appeared = self.session.wait_for(
                f"document.querySelectorAll({_js_list('chat_rows')}.join(',')).length >= 5",
                timeout=timeout,
            )
            if not appeared:
                self.log(f"第 {attempt} 次没等到会话列表（可能还在加载，只有占位行）")
                time.sleep(2.0)
                continue

            # 先定位行号，再用真实鼠标点它（优先点行内部的正文区域）；
            # 列表是渐进的，匹配不上就等一会儿再看，不急着放弃
            found = ""
            for _ in range(6):
                found = self.session.evaluate(
                    f"""(() => {{
                      const rows = {rows_expr};
                      if (rows.length < 5) return '';
                      // BOSS 上的公司名常常被截断成"泉州市洛江区双阳..."，所以要按
                      // 去掉省略号后的前缀、以及前 4 个字分别试
                      const raw = {json.dumps(company or "", ensure_ascii=False)};
                      const keys = [raw, raw.replace(/\\.\\.\\.$/, ''), raw.slice(0, 4)]
                        .filter(k => k && k.length >= 2);
                      let index = 0;
                      if (keys.length) {{
                        const hit = rows.findIndex(r =>
                          keys.some(k => (r.innerText || '').includes(k)));
                        // 给定了公司名却匹配不上，就不许乱选第一条，宁可不发
                        if (hit < 0) return '';
                        index = hit;
                      }}
                      return JSON.stringify({{index, count: rows.length,
                        text: (rows[index].innerText || '').trim().slice(0, 60)}});
                    }})()""",
                    await_promise=False,
                )
                if found:
                    break
                time.sleep(2.0)
            if found:
                info = json.loads(found)
                row_expr = (
                    f"(() => {{ const rows = {rows_expr}; const row = rows[{info['index']}];"
                    " if (!row) return null;"
                    " return row.querySelector('.text')"
                    " || row.querySelector('.friend-content-warp')"
                    " || row.querySelector('.title-box') || row; })()"
                )
                self.click_expr(row_expr)
                # 选完还要等输入框真的渲染出来，否则后面照样写不进去
                if self.session.wait_for(
                    f"!!document.querySelector({_js_list('chat_input')}.join(','))",
                    timeout=12,
                ):
                    return str(info["text"])
                self.log(f"第 {attempt} 次选中了会话但输入框没出来，重试")
            time.sleep(2.0)
        return ""

    def send_text(self, text: str, *, verify_seconds: float = 15.0) -> dict[str, Any]:
        """在当前会话里发一条消息，发完回读校验。返回 {pasted, sent, verified}。

        2026-10-15 加了两道闸，针对"同一段话术发两遍"这个真实事故：
        1. 发送前先读会话，这条话术已经在里面 → 直接算已发送，不重复发；
        2. 只有回读没通过、而且输入框里还留着这条话术时才按回车兜底，
           回车之后必须再回读一次，仍不通过就标 unverified（状态不明），
           由调用方按"状态不明"处理，绝不自动重发。
        """
        result: dict[str, Any] = {"pasted": "", "sent": "", "verified": False}

        has_input = self.session.wait_for(
            f"!!document.querySelector({_js_list('chat_input')}.join(','))", timeout=8
        )
        if not has_input:
            snapshot = self.dump_html("send-noinput")
            raise AdapterError(f"没等到聊天输入框，快照: {snapshot}")

        # --- 闸门一：发送前核对会话，已经发过就不发 ---
        history = self.conversation_history()
        if not history.get("pane"):
            time.sleep(1.0)
            history = self.conversation_history()
        if not history.get("pane"):
            snapshot = self.dump_html("send-nopane")
            raise AdapterError(f"读取当前会话内容失败，无法核对会话归属与防重历史，为防止误发已拒绝发送。快照: {snapshot}")

        decision = guards.decide_send(history, text)
        if decision["action"] == "skip":
            result["deduped"] = True
            result["sent"] = "skipped"
            result["verified"] = True
            result["input_cleared"] = True
            result["note"] = decision["reason"]
            self.log("发送前核对：会话里已经有同一条话术，跳过发送")
            return result

        # contenteditable 用 execCommand 写入最稳：dispatch paste 在部分版本不生效
        pasted = self.session.evaluate(
            f"""(() => {{
              const el = document.querySelector({_js_list('chat_input')}.join(','));
              if (!el) return 'no-input';
              el.focus();
              const text = {json.dumps(text, ensure_ascii=False)};
              if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {{
                el.value = text;
              }} else {{
                const ok = document.execCommand('insertText', false, text);
                if (!ok) el.innerText = text;
              }}
              el.dispatchEvent(new InputEvent('input', {{bubbles: true}}));
              return 'ok:' + ((el.innerText || el.value || '').length);
            }})()""",
            await_promise=False,
        )
        if not str(pasted).startswith("ok:"):
            raise AdapterError(f"写入输入框失败：{pasted}")
        result["pasted"] = pasted

        # 等发送按钮去掉 disabled：写入之后 Vue 是异步启用它的，
        # 只等 0.6 秒就点等于点了个灰按钮（实测就是这么丢掉 9 条的）
        ready = False
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            cls = self.session.evaluate(
                f"(() => {{ for (const s of {_js_list('chat_send')}) {{"
                "   const el = document.querySelector(s);"
                "   if (el) return String(el.className || ''); } return ''; })()",
                await_promise=False,
            )
            if cls and "disabled" not in str(cls):
                ready = True
                break
            time.sleep(0.3)

        sent = ""
        if ready:
            sent = self.session.evaluate(
                f"""(() => {{
                  for (const s of {_js_list('chat_send')}) {{
                    const el = document.querySelector(s);
                    if (el) {{ el.click(); return s; }}
                  }}
                  return '';
                }})()""",
                await_promise=False,
            )
        result["sent"] = str(sent)

        # 回读校验：正文里要能看到这条话，而且输入框必须回到空——
        # 只看"正文包含"会误判：文本还留在输入框里也会被算成发成功（实测踩过）
        if self._wait_delivered(text, verify_seconds):
            result["verified"] = True
            result["input_cleared"] = True

        # --- 闸门二：回读没通过时，只在输入框还留着这条话术的情况下才按回车兜底 ---
        # （输入框空了却"没读到"，说明已经发出去了，再按一次回车就是对空输入框乱按；
        #   历史上这里按下去过，同一段话术因此发了两遍）
        if not result["verified"] and not sent:
            drafted = self._input_length()
            if drafted > 0:
                self._press_enter()
                sent = "enter-key"
                result["sent"] = sent
                if self._wait_delivered(text, verify_seconds):
                    result["verified"] = True
                    result["input_cleared"] = True
            else:
                result["note"] = "没找到发送按钮，输入框也是空的，没有按下回车（避免重复发送）"

        if not result["verified"]:
            # 最后再整段回读一次会话：只要我方气泡里已经出现这条话术，就算已送达。
            # 这是"发送状态不明时先读聊天记录"的兜底，也是不重复发的前提。
            final = self.conversation_history()
            if guards.already_sent(final.get("mine") or [], text):
                result["verified"] = True
                result["input_cleared"] = True
                result["note"] = "回读校验超时，但会话里已出现该话术，按已送达处理"
            else:
                result["snapshot"] = self.dump_html("send-unverified")
                result.setdefault("note", "发送状态不明确：输入框与正文都没确认到，不要直接重发")
        return result

    # ---------- 发送相关的原子动作 ----------

    def _input_length(self) -> int:
        """当前输入框里的文字长度；-1 表示没找到输入框。"""
        value = self.session.evaluate(
            "(() => {"
            f" const el = document.querySelector({_js_list('chat_input')}.join(','));"
            " return el ? ((el.innerText || el.value || '').trim().length) : -1; })()",
            await_promise=False,
        )
        try:
            return int(value)
        except (TypeError, ValueError):
            return -1

    def _press_enter(self) -> None:
        """用 CDP 发一次真实回车（界面写着"按Enter键发送"）。"""
        self.session.call("Input.dispatchKeyEvent", {
            "type": "keyDown", "key": "Enter", "code": "Enter",
            "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13, "text": "\r",
        })
        self.session.call("Input.dispatchKeyEvent", {
            "type": "keyUp", "key": "Enter", "code": "Enter",
            "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13,
        })

    def _wait_delivered(self, text: str, verify_seconds: float) -> bool:
        """轮询：正文里出现这条话术，且输入框已清空。"""
        marker = text.strip().splitlines()[0][:14] if text.strip() else ""
        if not marker:
            return False
        deadline = time.monotonic() + verify_seconds
        while time.monotonic() < deadline:
            state = self.session.evaluate(
                "(() => {"
                f" const el = document.querySelector({_js_list('chat_input')}.join(','));"
                " const drafted = el ? ((el.innerText || el.value || '').trim().length) : -1;"
                # 只看会话面板，不看整页：整页 innerText 在大页面上很贵（2026-10-16 提速）
                " const pane = document.querySelector('.chat-conversation') || document.body;"
                f" const hit = (pane.innerText || '').includes({json.dumps(marker, ensure_ascii=False)});"
                " return JSON.stringify({drafted, hit}); })()",
                await_promise=False,
            )
            data = json.loads(state or "{}")
            if data.get("hit") and data.get("drafted") == 0:
                return True
            time.sleep(0.6)
        return False

    def send_greeting(self, job: dict[str, Any], text: str,
                      *, dry_run: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {"job": job.get("external_id"), "dry_run": dry_run}
        url = job.get("url")
        if url:
            self.session.navigate(url)
            # 2026-10-16 提速：不等整页 ready，等"立即沟通"按钮出现就够了
            # （一个公司省 5-10 秒，一批 10 家就是一分多钟）
            self.session.wait_for(
                f"document.querySelector({_js_list('start_chat')}.join(',')) is not null",
                timeout=12,
            )

        block = self.check_block()
        if block:
            snapshot = self.dump_html("send-blocked")
            raise BlockedError(f"发消息前被拦住：{block}，快照: {snapshot}")

        clicked = self.session.evaluate(
            f"""(() => {{
              for (const s of {_js_list('start_chat')}) {{
                const el = document.querySelector(s);
                if (el) {{ el.click(); return s; }}
              }}
              return '';
            }})()""",
            await_promise=False,
        )
        if not clicked:
            snapshot = self.dump_html("send-nobutton")
            raise AdapterError(f"没找到『立即沟通』按钮，快照: {snapshot}")
        result["clicked"] = clicked
        self.log(f"点了沟通按钮（{clicked}）")

        # 点完按钮 BOSS 只会发出账号里设置的默认招呼语，真正的定制话术必须补发。
        # 点按钮后页面一般会自己跳到 /web/geek/chat 并选中这个会话，那就直接用；
        # 只有在输入框没出来时，才回聊天列表按公司名点选。
        time.sleep(0.5)
        company = str(job.get("company") or "")
        keys = self._company_keys(company)
        picked = ""
        opened = self.session.wait_for(
            f"!!document.querySelector({_js_list('chat_input')}.join(','))", timeout=12
        )
        if opened:
            # 输入框在，还要确认面板里就是这家公司，免得把话发给上一条会话
            pane = self.current_conversation_text()
            if not keys or any(key in pane for key in keys):
                picked = "自动打开的会话（面板已核对）"
                self.log("页面已自动打开该会话，面板核对通过")
            else:
                self.log("输入框在但面板不是这家公司，回到列表重新点选")
        if not picked:
            picked = self.open_conversation(company)
        if not picked:
            snapshot = self.dump_html("send-noconversation")
            raise AdapterError(f"聊天页没选中会话（公司「{company}」），快照: {snapshot}")
        result["conversation"] = picked
        self.log(f"选中会话：{picked[:30]}")

        if dry_run:
            result["status"] = "dry-run"
            result["note"] = "已确认按钮、会话和输入框都在；--send 才会真的发出去"
            return result

        sent = self.send_text(text)
        result.update(sent)
        result["status"] = "sent" if sent["verified"] else "sent-unverified"
        return result

    # ---------- 简历附件请求与安全自动同意 ----------

    def current_account_info(self) -> dict[str, Any]:
        """读取当前登录的账号信息，确认登录状态与身份。"""
        info_js = """
        (() => {
          const userNode = document.querySelector('.nav-figure, .user-nav, .user-info, [class*="nav-user"]');
          if (!userNode) return JSON.stringify({logged_in: false, user_name: ""});
          const nameEl = document.querySelector('.user-nav .name, .header-user-name, [class*="user-name"]');
          return JSON.stringify({
            logged_in: true,
            user_name: nameEl ? (nameEl.innerText || '').trim() : "已登录用户"
          });
        })()
        """
        try:
            raw = self.session.evaluate(info_js, await_promise=False)
            return json.loads(raw)
        except Exception:
            return {"logged_in": False, "user_name": ""}

    def detect_resume_request(self, company: str = "", hr_name: str = "") -> dict[str, Any]:
        """扫描当前会话是否包含 HR 发起的简历附件投递请求，并附加稳定卡片指纹。"""
        raw = self.session.evaluate(RESUME_REQUEST_JS, await_promise=False)
        try:
            data = json.loads(raw)
            if data.get("ok"):
                for card in data.get("cards", []):
                    if not card.get("fingerprint"):
                        card["fingerprint"] = compute_card_fingerprint(
                            company, hr_name, card.get("snippet", "")
                        )
            return data
        except Exception as exc:
            return {"ok": False, "message": f"解析失败: {exc}", "cards": []}

    def accept_resume_request(
        self,
        company: str,
        hr_name: str | None = None,
        *,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """安全识别并同意 HR 发起的简历附件请求。严格履行卡片指纹与多重状态核验。"""
        result: dict[str, Any] = {
            "company": company,
            "hr_name": hr_name,
            "dry_run": dry_run,
            "status": "pending",
            "verified": False,
            "fingerprint": "",
        }

        # 准则 1：风控与登录态严格核查
        block = self.check_block()
        if block:
            snapshot = self.dump_html("accept-resume-blocked")
            raise BlockedError(f"操作前检测到登录或风控阻断: {block}，快照: {snapshot}")

        # 准则 2：会话面板严格存在性与归属核查（必须确认当前打开的是正确的目标公司与HR）
        history = self.conversation_history()
        if not history.get("pane"):
            snapshot = self.dump_html("accept-resume-nopane")
            raise AdapterError(f"未能读取到聊天会话面板，无法确认会话上下文，已终止操作。快照: {snapshot}")

        keys = self._company_keys(company)
        current_text = f"{history.get('company', '')} {history.get('name', '')} {history.get('job', '')}"
        if keys and not any(k in current_text for k in keys):
            # 若当前面板不匹配目标公司，尝试切换到目标会话
            self.log(f"当前会话（{current_text[:30]}）与目标公司（{company}）不一致，尝试从列表重新选中")
            picked = self.open_conversation(company)
            if not picked:
                snapshot = self.dump_html("accept-resume-wrong-company")
                raise AdapterError(f"无法定位到目标公司「{company}」的会话，为防误同意已终止操作。快照: {snapshot}")
            history = self.conversation_history()
            if not history.get("pane"):
                snapshot = self.dump_html("accept-resume-reopen-nopane")
                raise AdapterError(f"重新打开会话后读取面板失败。快照: {snapshot}")

        # 严格验证 HR 身份
        current_hr = str(history.get("name", "")).strip()
        if hr_name and current_hr:
            if hr_name.strip() not in current_hr and current_hr not in hr_name.strip():
                snapshot = self.dump_html("accept-resume-wrong-hr")
                raise AdapterError(f"当前会话中的 HR 姓名「{current_hr}」与目标 HR「{hr_name}」不匹配，操作终止。快照: {snapshot}")

        # 准则 3 & 4：区分结构化卡片与普通聊天，扫描卡片并建立卡片身份指纹
        detect_res = self.detect_resume_request(company=company, hr_name=current_hr or (hr_name or ""))
        if not detect_res.get("ok"):
            snapshot = self.dump_html("accept-resume-detect-err")
            raise AdapterError(f"检测简历请求卡片失败: {detect_res.get('message')}，快照: {snapshot}")

        cards = detect_res.get("cards", [])
        if not cards:
            result["status"] = "no_request"
            result["note"] = "当前会话未检测到 HR 发起的简历附件请求卡片（普通聊天消息不触发自动同意）"
            return result

        # 筛选待处理的卡片（不盲目使用 cards[-1]）
        def _get_fp(c: dict[str, Any]) -> str:
            return c.get("fingerprint") or compute_card_fingerprint(
                company, current_hr or (hr_name or ""), c.get("snippet", "")
            )

        pending_cards = [c for c in cards if not c.get("is_already_accepted")]
        if not pending_cards:
            result["status"] = "already_accepted"
            result["verified"] = True
            result["fingerprint"] = _get_fp(cards[-1])
            result["note"] = "该会话中的所有简历请求此前已处于已同意状态，已自动防重跳过"
            return result

        target_card = pending_cards[-1]
        target_fingerprint = _get_fp(target_card)
        result["fingerprint"] = target_fingerprint

        # 准则 5：校验同意按钮存在性
        if not target_card.get("has_agree_button"):
            snapshot = self.dump_html("accept-resume-nobtn")
            raise AdapterError(f"识别到简历请求但未定位到安全同意按钮（可能已被处理或结构变动），快照: {snapshot}")

        btn_selector = target_card.get("button_selector") or ""
        btn_text = (target_card.get("agree_button_text") or "").strip()
        result["button_text"] = btn_text

        # 按钮文本安全白名单校验（绝不允许点击拒绝/取消/关闭或未知按钮）
        safe_agree_keywords = ("同意", "发送附件", "立即发送", "发送")
        forbidden_keywords = ("拒绝", "不合适", "取消", "关闭")
        if any(bad in btn_text for bad in forbidden_keywords):
            snapshot = self.dump_html("accept-resume-forbidden-btn")
            raise AdapterError(f"严禁点击包含拒绝/取消文案的按钮（识别为『{btn_text}』），快照: {snapshot}")
        if not any(good in btn_text for good in safe_agree_keywords):
            snapshot = self.dump_html("accept-resume-unknown-btn")
            raise AdapterError(f"按钮文案『{btn_text}』未匹配已知安全同意文案，已拒绝点击，快照: {snapshot}")

        # 准则 6：Dry-run 保护
        if dry_run:
            result["status"] = "dry-run"
            result["note"] = f"已成功识别到简历请求卡片与同意按钮『{btn_text}』(指纹:{target_fingerprint[:8]})；dry_run 模式未实际点击"
            return result

        # 准则 7：点击前重新验证公司、HR、当前聊天会话、卡片状态、按钮选择器和按钮文案
        pre_hist = self.conversation_history()
        if not pre_hist.get("pane"):
            snapshot = self.dump_html("accept-resume-precheck-nopane")
            raise AdapterError(f"点击前会话面板丢失，操作终止，快照: {snapshot}")

        pre_text = f"{pre_hist.get('company', '')} {pre_hist.get('name', '')} {pre_hist.get('job', '')}"
        if keys and not any(k in pre_text for k in keys):
            snapshot = self.dump_html("accept-resume-precheck-wrong-comp")
            raise AdapterError(f"点击前会话公司身份发生漂移，操作终止，快照: {snapshot}")

        if hr_name and pre_hist.get("name"):
            pre_hr = str(pre_hist.get("name", "")).strip()
            if hr_name.strip() not in pre_hr and pre_hr not in hr_name.strip():
                snapshot = self.dump_html("accept-resume-precheck-wrong-hr")
                raise AdapterError(f"点击前会话 HR 身份发生漂移，操作终止，快照: {snapshot}")

        precheck = self.detect_resume_request(company=company, hr_name=current_hr or (hr_name or ""))
        if not precheck.get("ok"):
            snapshot = self.dump_html("accept-resume-precheck-fail")
            raise AdapterError(f"点击前页面结构发生变化或读取失败: {precheck.get('message')}，快照: {snapshot}")

        precheck_cards = precheck.get("cards", [])
        if not precheck_cards:
            snapshot = self.dump_html("accept-resume-card-disappeared")
            raise AdapterError(f"点击前简历请求卡片已在页面消失，操作终止，快照: {snapshot}")

        # 严格按卡片指纹寻找目标同一张卡片（绝不盲目依靠 cards[-1]）
        matched = [c for c in precheck_cards if _get_fp(c) == target_fingerprint]
        if not matched:
            snapshot = self.dump_html("accept-resume-card-mismatch")
            raise AdapterError(f"点击前未在页面中匹配到同一张简历请求卡片（卡片指纹不一致或已消失），操作终止，快照: {snapshot}")

        active_card = matched[0]
        if active_card.get("is_already_accepted"):
            result["status"] = "already_accepted"
            result["verified"] = True
            result["note"] = "点击前二次核验证实该请求已处于已同意状态，已自动跳过"
            return result

        if not active_card.get("has_agree_button"):
            snapshot = self.dump_html("accept-resume-agree-disappeared")
            raise AdapterError(f"点击前同意按钮已消失或失效，操作终止，快照: {snapshot}")

        # 重新核验按钮选择器与文案
        active_btn_selector = active_card.get("button_selector") or btn_selector
        active_btn_text = (active_card.get("agree_button_text") or btn_text).strip()
        if any(bad in active_btn_text for bad in forbidden_keywords):
            snapshot = self.dump_html("accept-resume-precheck-bad-btn")
            raise AdapterError(f"点击前按钮状态变异包含拒绝/取消语义（『{active_btn_text}』），操作终止，快照: {snapshot}")
        if not any(good in active_btn_text for good in safe_agree_keywords):
            snapshot = self.dump_html("accept-resume-precheck-unknown-btn")
            raise AdapterError(f"点击前按钮文案发生变动（『{active_btn_text}』），非已知安全文案，操作终止，快照: {snapshot}")

        # 准则 8：执行点击（使用 CSS 选择器严格调用 click_selector）
        click_res = self.click_selector(active_btn_selector)
        if not click_res.get("clicked"):
            snapshot = self.dump_html("accept-resume-click-fail")
            raise AdapterError(f"点击同意按钮失败: {click_res.get('error')}，快照: {snapshot}")

        # 准则 9：点击后重新读取页面确认真实状态（等待 1.5s 后核验状态变为已同意）
        time.sleep(1.5)
        verify_detect = self.detect_resume_request(company=company, hr_name=current_hr or (hr_name or ""))
        if not verify_detect.get("ok"):
            result["status"] = "clicked-unverified"
            result["verified"] = False
            result["note"] = f"点击后页面结构发生变化或回读失败 ({verify_detect.get('message')})，已停止后续操作并标记为未确认状态"
            return result

        # 重新查找目标卡片验证真实状态
        new_matched = [c for c in verify_detect.get("cards", []) if _get_fp(c) == target_fingerprint]
        verified = False
        if new_matched:
            latest = new_matched[0]
            if latest.get("is_already_accepted") or not latest.get("has_agree_button"):
                verified = True
        else:
            # 卡片在同意后可能转变为普通系统消息
            verified = True

        result["status"] = "accepted" if verified else "clicked-unverified"
        result["verified"] = verified
        result["note"] = (
            "已同意发送简历附件并通过状态校验"
            if verified
            else "已执行点击但未能确证页面状态更新，已安全标记为未确认状态，防止重复点击"
        )
        return result
