"""大模型调用接口（可选）。

默认复用本机 Codex 已经配好的 DeepSeek，凭据现读现用：
- 优先环境变量 DEEPSEEK_API_KEY
- 否则读 ~/.codex/config.toml 里 model_providers.<provider> 的 token
密钥不会写进本仓库、不会进日志、不会进简历。
不可用时返回明确错误，调用方退回规则引擎，流程不中断。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from . import voice
from .config import LLMSettings, load as load_config

DEFAULT_MODELS = {
    "deepseek": "deepseek-chat",
}


@dataclass
class LLMResult:
    ok: bool
    text: str = ""
    model: str = ""
    error: str = ""
    seconds: float = 0.0
    usage: dict[str, Any] | None = None


class LLM:
    def __init__(self, settings: LLMSettings | None = None):
        self.settings = settings or load_config().llm
        resolved = self.settings.resolve()
        self.base_url = resolved["base_url"]
        self.api_key = resolved["api_key"]
        model = self.settings.model or DEFAULT_MODELS.get(self.settings.provider, "")
        if self.settings.provider == "deepseek" and model not in ("deepseek-chat", "deepseek-reasoner"):
            # Codex 里的 deepseek-flash 是客户端别名，直连 API 要用官方模型名
            model = DEFAULT_MODELS["deepseek"]
        self.model = model

    def available(self) -> tuple[bool, str]:
        if not self.settings.enabled:
            return False, "config.toml 里 [llm].enabled = false，已物理关闭外部模型调用"
        if not getattr(self.settings, "allow_paid_api", False):
            if any(domain in self.base_url for domain in ["api.deepseek.com", "openai.com", "anthropic.com", "zhipu", "moonshot"]):
                return False, "防偷跑安全拦截：未开启 allow_paid_api，已彻底禁止向外部公网计费模型发起请求"
        if not self.base_url:
            return False, "没找到 base_url：外部调用已关闭"
        if not self.api_key:
            return False, "外部计费 API 凭据已封堵，系统走本地纯规则降级"
        return True, f"可用：{self.base_url}，模型 {self.model}"

    def chat(self, messages: list[dict[str, str]], *, temperature: float | None = None,
             max_tokens: int | None = None, timeout: float | None = None,
             inject_rules: bool = True, register: str = "对外文字") -> LLMResult:
        # 防偷跑物理熔断：未允许付费 API 时直接阻断
        if not getattr(self.settings, "allow_paid_api", False):
            if any(domain in self.base_url for domain in ["api.deepseek.com", "openai.com", "anthropic.com", "zhipu", "moonshot"]):
                return LLMResult(ok=False, error="防偷跑安全熔断：已阻止向外部计费域名发送请求，自动使用本地规则处理")
        ok, detail = self.available()
        if not ok:
            return LLMResult(ok=False, error=detail)
        url = self.base_url.rstrip("/") + "/chat/completions"
        msgs = list(messages)
        if inject_rules and not any((m.get("role") or "") == "system" for m in msgs):
            # 全局自然表达规则的紧凑摘要；只在已经有模型调用的步骤上加，不新增调用。
            msgs = [{"role": "system", "content": voice.system_prompt(register)}] + msgs
        payload = {
            "model": self.model,
            "messages": msgs,
            "temperature": self.settings.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.settings.max_output_tokens,
            "stream": False,
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "jobagent/0.2",
            },
        )
        started = time.time()
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.settings.timeout) as response:
                body = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")[:300]
            hint = ""
            if exc.code in (401, 403):
                hint = "（密钥无效或没有权限）"
            elif exc.code == 429:
                hint = "（触发限流，稍后再试）"
            return LLMResult(ok=False, error=f"模型接口返回 HTTP {exc.code}{hint}：{raw}")
        except Exception as exc:
            return LLMResult(ok=False, error=f"模型接口调用失败：{type(exc).__name__}: {exc}")
        choices = body.get("choices") or []
        text = ""
        if choices:
            text = ((choices[0].get("message") or {}).get("content") or "").strip()
        return LLMResult(ok=bool(text), text=text, model=body.get("model", self.model),
                         seconds=time.time() - started, usage=body.get("usage"),
                         error="" if text else "接口返回里没有内容")

    # ---------- 面向任务的两层封装 ----------

    def health_check(self) -> LLMResult:
        return self.chat([{"role": "user", "content": "回复两个字：可用"}],
                         max_tokens=16, inject_rules=False)

    def review_resume(self, *, direction: str, text: str) -> LLMResult:
        """以招聘初筛视角挑最可能导致淘汰的问题。"""
        prompt = (
            f"你是一名正在筛简历的招聘方。下面这份简历投的是「{direction}」方向的岗位。\n"
            "只指出最可能导致淘汰的三个问题，每条一行，说明问题出在哪句，并给出改法。\n"
            "不要客套，不要总结，不要编造简历里没有的事实。如果某处事实无法确认，直接说需要确认。\n\n"
            f"{text}"
        )
        return self.chat([{"role": "user", "content": prompt}], temperature=0.2)

    def polish(self, *, text: str, register: str) -> LLMResult:
        prompt = (
            f"把下面这段中文改得更自然、更贴合「{register}」的语域。\n"
            "硬要求：保留全部事实、数字、名称和立场，一个都不能改；不要添加原文没有的经历；\n"
            "去掉套话、固定并列结构和无信息量的总结；句长自然变化；只输出改写后的正文。\n\n"
            f"{text}"
        )
        return self.chat([{"role": "user", "content": prompt}], temperature=0.4)

    def extract_requirements(self, job_text: str) -> LLMResult:
        prompt = (
            "从下面的招聘 JD 里抽出「任职要求」条目，每条一行，保留原始措辞，不要改写、不要补充。\n"
            "如果 JD 里没有明确的要求段，就抽出最能体现能力要求的句子。\n\n"
            f"{job_text}"
        )
        return self.chat([{"role": "user", "content": prompt}], temperature=0.1)
