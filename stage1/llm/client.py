# -*- coding: utf-8 -*-
"""
llm/client.py —— 统一 LLM 客户端

设计要点：
1. 全部通过环境变量配置（LLM_PROVIDER / LLM_API_KEY / LLM_MODEL / LLM_BASE_URL ...），
   代码中不出现任何密钥。
2. 同时支持两种协议：
   - anthropic : Anthropic Messages API（POST /v1/messages）
   - openai    : OpenAI Chat Completions 兼容协议（POST {base}/chat/completions），
                 可对接 DeepSeek、Qwen(DashScope 兼容模式)、Moonshot、vLLM 等。
3. 所有业务调用都要求模型"只输出 JSON"，本客户端负责剥离 ```json 围栏并解析；
   解析失败自动带纠错提示重试（最多 LLM_MAX_RETRIES 次）。
4. API Key 只代表已配置。每次调用还必须携带本轮授权和具体任务，
   防止环境中存在密钥时自动向外部发送论文内容。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests

from config import Settings
from llm.policy import LLMAuthorization


class LLMError(Exception):
    """LLM 调用或响应解析失败。"""

    def __init__(
        self, message: str, *, code: str = "llm_error",
        usage: dict[str, Any] | None = None, response_model: str = "",
        finish_reason: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.usage = usage or {}
        self.response_model = response_model
        self.finish_reason = finish_reason


@dataclass(frozen=True, slots=True)
class LLMCallRecord:
    """不含提示词正文和密钥的单次外部调用遥测。"""

    attempt: int
    provider: str
    configured_model: str
    response_model: str
    finish_reason: str
    endpoint_host: str
    success: bool
    duration_seconds: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    prompt_cache_hit_tokens: int = 0
    prompt_cache_miss_tokens: int = 0
    reasoning_tokens: int = 0
    error_code: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class LLMClient:
    def __init__(self, settings: Settings):
        self.s = settings
        self._call_records: list[LLMCallRecord] = []

    # ------------------------------------------------------------------ #
    @property
    def configured(self) -> bool:
        return self.s.llm_configured

    @property
    def endpoint_host(self) -> str:
        return urlparse(self.s.llm_base_url).hostname or ""

    def describe(self) -> str:
        if not self.configured:
            return "未配置大语言模型调用能力"
        return f"{self.s.llm_provider} / {self.s.llm_model} @ {self.s.llm_base_url}"

    @property
    def call_records(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self._call_records]

    def usage_summary(self) -> dict[str, Any]:
        """汇总真实接口返回的 token 与实际网络等待时间。"""
        return {
            "call_count": len(self._call_records),
            "successful_calls": sum(item.success for item in self._call_records),
            "duration_seconds": round(
                sum(item.duration_seconds for item in self._call_records), 6
            ),
            "prompt_tokens": sum(item.prompt_tokens for item in self._call_records),
            "completion_tokens": sum(
                item.completion_tokens for item in self._call_records
            ),
            "total_tokens": sum(item.total_tokens for item in self._call_records),
            "prompt_cache_hit_tokens": sum(
                item.prompt_cache_hit_tokens for item in self._call_records
            ),
            "prompt_cache_miss_tokens": sum(
                item.prompt_cache_miss_tokens for item in self._call_records
            ),
            "reasoning_tokens": sum(
                item.reasoning_tokens for item in self._call_records
            ),
        }

    # ------------------------------------------------------------------ #
    def _ensure_authorized(self, authorization: LLMAuthorization, task: str) -> None:
        if not authorization.allows_task(task):
            raise LLMError("本轮未授权该大语言模型任务", code="task_not_authorized")
        if not authorization.external_processing_allowed:
            raise LLMError("本轮未确认外部数据处理", code="external_processing_not_allowed")
        if not self.configured:
            raise LLMError("服务器未配置 LLM_API_KEY", code="not_configured")

    def complete_text(
        self,
        system: str,
        user: str,
        *,
        authorization: LLMAuthorization,
        task: str,
    ) -> str:
        """在授权门禁后调用文本接口，并统一传播网络和接口失败。"""
        self._ensure_authorized(authorization, task)
        last_err: Exception | None = None
        for attempt in range(self.s.llm_max_retries + 1):
            started = time.monotonic()
            try:
                text, usage, response_model = self._complete_text(system, user)
                self._record_call(
                    attempt=attempt + 1, started=started, success=True,
                    usage=usage, response_model=response_model,
                )
                return text
            except requests.RequestException as exc:
                last_err = LLMError(str(exc), code="network_error")
            except LLMError as exc:
                last_err = exc
            self._record_call(
                attempt=attempt + 1, started=started, success=False,
                error_code=getattr(last_err, "code", "llm_error"),
                usage=getattr(last_err, "usage", {}),
                response_model=getattr(last_err, "response_model", ""),
                finish_reason=getattr(last_err, "finish_reason", ""),
            )
            if attempt < self.s.llm_max_retries:
                time.sleep(min(2 ** attempt, 8))
        if isinstance(last_err, LLMError):
            raise last_err
        raise LLMError("大语言模型调用失败", code="llm_error")

    def complete_json(
        self,
        system: str,
        user: str,
        *,
        authorization: LLMAuthorization,
        task: str,
    ) -> Dict[str, Any]:
        """调用 LLM 并解析其 JSON 输出。失败时抛出 LLMError。"""
        last_err: Optional[Exception] = None
        prompt = user
        for attempt in range(self.s.llm_max_retries + 1):
            try:
                text = self.complete_text(
                    system,
                    prompt,
                    authorization=authorization,
                    task=task,
                )
                return self._parse_json(text)
            except LLMError as e:
                if e.code != "response_parse_error":
                    raise
                last_err = e
                prompt = user + (
                    f"\n\n注意：你上一次的输出无法被解析为 JSON（{e}）。"
                    "请只输出一个合法 JSON 对象，不要包含其他文字或 Markdown 围栏。"
                )
                time.sleep(min(2 ** attempt, 8))
            except json.JSONDecodeError as e:  # noqa: PERF203
                last_err = e
                prompt = user + (
                    f"\n\n注意：你上一次的输出无法被解析为 JSON（{e}）。"
                    "请只输出一个合法 JSON 对象，不要包含其他文字或 Markdown 围栏。"
                )
                time.sleep(min(2 ** attempt, 8))
        raise LLMError(
            f"LLM JSON 解析失败（已重试 {self.s.llm_max_retries} 次）：{last_err}",
            code="response_parse_error",
        )

    def complete_with_tools(
        self,
        system: str,
        user: str,
        tools: list[dict[str, Any]],
        *,
        authorization: LLMAuthorization,
        task: str,
    ) -> dict[str, Any]:
        """调用提供商原生 function calling，并归一化工具请求或最终建议。"""
        self._ensure_authorized(authorization, task)
        started = time.monotonic()
        try:
            if self.s.llm_provider == "anthropic":
                result, usage, response_model, finish_reason = (
                    self._call_anthropic_with_tools(system, user, tools)
                )
            else:
                result, usage, response_model, finish_reason = (
                    self._call_openai_with_tools(system, user, tools)
                )
            self._record_call(
                attempt=1, started=started, success=True, usage=usage,
                response_model=response_model, finish_reason=finish_reason,
            )
            return result
        except requests.RequestException as exc:
            error = LLMError(str(exc), code="network_error")
        except LLMError as exc:
            error = exc
        self._record_call(
            attempt=1, started=started, success=False,
            error_code=error.code, usage=error.usage,
            response_model=error.response_model,
            finish_reason=error.finish_reason,
        )
        raise error

    # ------------------------------------------------------------------ #
    def _record_call(
        self, *, attempt: int, started: float, success: bool,
        usage: dict[str, Any] | None = None, response_model: str = "",
        error_code: str = "", finish_reason: str = "",
    ) -> None:
        usage = usage or {}
        details = usage.get("completion_tokens_details") or {}
        self._call_records.append(LLMCallRecord(
            attempt=attempt, provider=self.s.llm_provider,
            configured_model=self.s.llm_model, response_model=response_model,
            finish_reason=finish_reason,
            endpoint_host=self.endpoint_host, success=success,
            duration_seconds=round(time.monotonic() - started, 6),
            prompt_tokens=int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
            completion_tokens=int(
                usage.get("completion_tokens") or usage.get("output_tokens") or 0
            ),
            total_tokens=int(usage.get("total_tokens") or 0),
            prompt_cache_hit_tokens=int(usage.get("prompt_cache_hit_tokens") or 0),
            prompt_cache_miss_tokens=int(usage.get("prompt_cache_miss_tokens") or 0),
            reasoning_tokens=int(details.get("reasoning_tokens") or 0),
            error_code=error_code,
        ))

    def _complete_text(
        self, system: str, user: str,
    ) -> tuple[str, dict[str, Any], str]:
        if self.s.llm_provider == "anthropic":
            return self._call_anthropic(system, user)
        return self._call_openai(system, user)

    def _call_anthropic(
        self, system: str, user: str,
    ) -> tuple[str, dict[str, Any], str]:
        url = f"{self.s.llm_base_url}/v1/messages"
        headers = {
            "x-api-key": self.s.llm_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": self.s.llm_model,
            "max_tokens": self.s.llm_max_tokens,
            "temperature": self.s.llm_temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        resp = requests.post(url, headers=headers, json=body, timeout=self.s.llm_timeout)
        if resp.status_code != 200:
            # 不把服务端响应正文写入日志，避免其回显提示词或论文片段。
            raise LLMError(f"Anthropic API HTTP {resp.status_code}", code="http_error")
        data = resp.json()
        usage = data.get("usage") or {}
        response_model = str(data.get("model") or "")
        parts = [blk.get("text", "") for blk in data.get("content", []) if blk.get("type") == "text"]
        text = "".join(parts).strip()
        if not text:
            raise LLMError(
                "Anthropic API 返回了空文本", code="empty_response",
                usage=usage, response_model=response_model,
                finish_reason=str(data.get("stop_reason") or ""),
            )
        return text, usage, response_model

    def _call_openai(
        self, system: str, user: str,
    ) -> tuple[str, dict[str, Any], str]:
        url = f"{self.s.llm_base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.s.llm_api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.s.llm_model,
            "temperature": self.s.llm_temperature,
            "max_tokens": self.s.llm_max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        resp = requests.post(url, headers=headers, json=body, timeout=self.s.llm_timeout)
        if resp.status_code != 200:
            raise LLMError(f"OpenAI 兼容 API HTTP {resp.status_code}", code="http_error")
        data = resp.json()
        usage = data.get("usage") or {}
        response_model = str(data.get("model") or "")
        choices = data.get("choices") or []
        finish_reason = str(choices[0].get("finish_reason") or "") if choices else ""
        try:
            text = (data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError, AttributeError) as e:
            raise LLMError(
                f"OpenAI 兼容 API 响应结构异常: {e}",
                code="response_structure_error", usage=usage,
                response_model=response_model, finish_reason=finish_reason,
            ) from e
        if not text:
            raise LLMError(
                "OpenAI 兼容 API 返回了空文本", code="empty_response",
                usage=usage, response_model=response_model,
                finish_reason=finish_reason,
            )
        return text, usage, response_model

    def _call_openai_with_tools(
        self, system: str, user: str, tools: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any], str, str]:
        url = f"{self.s.llm_base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.s.llm_api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.s.llm_model,
            "temperature": self.s.llm_temperature,
            "max_tokens": self.s.llm_max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": item["name"],
                        "description": item.get("description", ""),
                        "parameters": item["input_schema"],
                    },
                }
                for item in tools
            ],
            "tool_choice": "auto",
        }
        if not tools:
            body.pop('tools')
            body.pop('tool_choice')
        response = requests.post(
            url, headers=headers, json=body, timeout=self.s.llm_timeout
        )
        if response.status_code != 200:
            raise LLMError(
                f"OpenAI 兼容 API HTTP {response.status_code}", code="http_error"
            )
        data = response.json()
        usage = data.get("usage") or {}
        response_model = str(data.get("model") or "")
        choices = data.get("choices") or []
        finish_reason = str(choices[0].get("finish_reason") or "") if choices else ""
        if not choices or not isinstance(choices[0].get("message"), dict):
            raise LLMError("OpenAI 工具响应结构异常", code="response_structure_error",
                usage=usage, response_model=response_model, finish_reason=finish_reason)
        message = choices[0]["message"]
        calls = []
        for item in message.get("tool_calls") or []:
            function = item.get("function") or {}
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError as exc:
                raise LLMError(
                    "OpenAI 工具参数不是合法 JSON", code="response_parse_error",
                    usage=usage, response_model=response_model, finish_reason=finish_reason,
                ) from exc
            calls.append({"name": str(function.get("name") or ""), "arguments": arguments})
        if calls:
            return {"tool_calls": calls}, usage, response_model, finish_reason
        content = str(message.get("content") or "")
        result = self._parse_terminal(content, usage=usage, response_model=response_model, finish_reason=finish_reason)
        return result, usage, response_model, finish_reason

    def _call_anthropic_with_tools(
        self, system: str, user: str, tools: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any], str, str]:
        url = f"{self.s.llm_base_url}/v1/messages"
        headers = {
            "x-api-key": self.s.llm_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": self.s.llm_model,
            "max_tokens": self.s.llm_max_tokens,
            "temperature": self.s.llm_temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "tools": tools,
        }
        if not tools:
            body.pop('tools')
        response = requests.post(
            url, headers=headers, json=body, timeout=self.s.llm_timeout
        )
        if response.status_code != 200:
            raise LLMError(f"Anthropic API HTTP {response.status_code}", code="http_error")
        data = response.json()
        usage = data.get("usage") or {}
        response_model = str(data.get("model") or "")
        finish_reason = str(data.get("stop_reason") or "")
        calls = [
            {"name": str(block.get("name") or ""), "arguments": block.get("input") or {}}
            for block in data.get("content", [])
            if block.get("type") == "tool_use"
        ]
        if calls:
            return {"tool_calls": calls}, usage, response_model, finish_reason
        text = "".join(
            str(block.get("text") or "") for block in data.get("content", [])
            if block.get("type") == "text"
        )
        result = self._parse_terminal(text, usage=usage, response_model=response_model, finish_reason=finish_reason)
        return result, usage, response_model, finish_reason

    @staticmethod
    def _parse_terminal(text: str, *, usage: dict, response_model: str, finish_reason: str) -> dict:
        """规划终态必须是独立 JSON 对象；协议失败也保留本次 Token 和调用事实。"""
        try:
            value = json.loads(text)
            if not isinstance(value, dict):
                raise ValueError('终态不是对象')
            return value
        except (TypeError, ValueError) as exc:
            raise LLMError('模型终态不是独立的合法 JSON 对象', code='response_parse_error',
                usage=usage, response_model=response_model, finish_reason=finish_reason) from exc

    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_json(text: str) -> Dict[str, Any]:
        """剥离 Markdown 围栏 / 前后杂讯后解析 JSON 对象。"""
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
        # 截取第一个 { 到最后一个 } 之间的内容，容忍模型输出少量前后缀文字
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise LLMError("响应中未找到 JSON 对象", code="response_parse_error")
        return json.loads(cleaned[start : end + 1])
