# -*- coding: utf-8 -*-
"""
config.py —— 全局配置

所有可调参数均通过【环境变量】读取（支持项目根目录下的 .env 文件），
不在代码中硬编码任何密钥。参见 .env.example。

环境变量一览：
  LLM_PROVIDER          LLM 提供方: "openai"（默认）| "anthropic"；OpenAI 兼容协议
                        可对接 DeepSeek / Qwen / Moonshot / 本地 vLLM 等任何兼容端点）
  LLM_API_KEY           API 密钥，只表示服务器具备调用能力，不代表本轮授权。
                        无论是否设置，流水线默认都不调用外部大语言模型。
  LLM_MODEL             模型名（openai 默认 deepseek-v4-pro）
  LLM_BASE_URL          API 基地址（openai 默认 https://api.deepseek.com）
  LLM_TEMPERATURE       采样温度，默认 0（结构化判断任务需要确定性）
  LLM_MAX_TOKENS        单次回复最大 token 数，默认 2048
  LLM_TIMEOUT           单次请求超时秒数，默认 120
  LLM_MAX_RETRIES       网络/JSON 解析失败重试次数，默认 2

  CONFIDENCE_THRESHOLD  章节分派置信度阈值，低于该值进入人工确认清单，默认 0.85
  MAX_COMPILE_ROUNDS    编译自愈最大迭代轮数，默认 5
  LATEX_COMPILER        编译器: xelatex（默认，中文文档必需）| pdflatex | lualatex
  REPAIR_RULES_PATH     修复规则库路径，默认 <项目>/rules/repair_rules.json
  SIGNAL_WORDS_PATH     语义歧义信号词配置，默认 <项目>/configs/signal_words.json
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """极简 .env 解析器（无第三方依赖）。已存在的环境变量不会被覆盖。"""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


# 依次尝试：当前工作目录的 .env → 项目目录的 .env
_load_dotenv(Path.cwd() / ".env")
_load_dotenv(PROJECT_ROOT / ".env")


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)))
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


@dataclass
class Settings:
    # ---- LLM ----
    llm_provider: str = field(default_factory=lambda: _env("LLM_PROVIDER", "openai").lower())
    llm_api_key: str = field(default_factory=lambda: _env("LLM_API_KEY"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL"))
    llm_base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL"))
    llm_temperature: float = field(default_factory=lambda: _env_float("LLM_TEMPERATURE", 0.0))
    llm_max_tokens: int = field(default_factory=lambda: _env_int("LLM_MAX_TOKENS", 2048))
    llm_timeout: int = field(default_factory=lambda: _env_int("LLM_TIMEOUT", 120))
    llm_max_retries: int = field(default_factory=lambda: _env_int("LLM_MAX_RETRIES", 2))

    # ---- 流程参数 ----
    confidence_threshold: float = field(default_factory=lambda: _env_float("CONFIDENCE_THRESHOLD", 0.85))
    max_compile_rounds: int = field(default_factory=lambda: _env_int("MAX_COMPILE_ROUNDS", 5))
    latex_compiler: str = field(default_factory=lambda: _env("LATEX_COMPILER", "xelatex"))
    repair_rules_path: Path = field(
        default_factory=lambda: Path(_env("REPAIR_RULES_PATH", str(PROJECT_ROOT / "rules" / "repair_rules.json")))
    )
    signal_words_path: Path = field(
        default_factory=lambda: Path(_env("SIGNAL_WORDS_PATH", str(PROJECT_ROOT / "configs" / "signal_words.json")))
    )

    def __post_init__(self):
        if self.llm_provider not in ("anthropic", "openai"):
            self.llm_provider = "openai"
        if not self.llm_model:
            self.llm_model = (
                "claude-sonnet-4-6"
                if self.llm_provider == "anthropic" else "deepseek-v4-pro"
            )
        if not self.llm_base_url:
            self.llm_base_url = (
                "https://api.anthropic.com"
                if self.llm_provider == "anthropic" else "https://api.deepseek.com"
            )
        self.llm_base_url = self.llm_base_url.rstrip("/")

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key)

    @property
    def llm_available(self) -> bool:
        """兼容旧调用；这里只表示已配置，不能作为调用授权。"""
        return self.llm_configured


def get_settings() -> Settings:
    return Settings()
