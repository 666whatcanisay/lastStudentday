"""
云端 LLM 客户端（OpenAI 兼容协议）

设计要点
--------
1. **永不抛异常**：所有方法都返回结果字典，网络错误 / 超时 / 401 / 429
   统一收敛成 `{"ok": False, "error": "..."}`。RAG 链路的降级由调用方决定，
   不靠异常控制流 —— 这样"没有 Key 也能跑通检索"是天然成立的，不是补丁。

2. **协议中立**：走标准 OpenAI /chat/completions 协议，
   换厂商（DeepSeek / 通义 / Kimi / 智谱 / OpenAI）只改 .env 里的
   LLM_BASE_URL 与 LLM_MODEL，代码零改动。

3. **绕开系统代理**：本机存在系统级 HTTP_PROXY（端口还会随会话变化，
   历史出现过 52392 / 55212）。一旦代理进程没起来，所有 LLM 调用会整体
   失败。因此默认 `trust_env=False` 直连，行为确定。
   若某网络环境确实必须走代理，在 .env 里设 LLM_USE_ENV_PROXY=true 即可。

4. **失败重试**：仅对网络类错误与 5xx 重试 1 次；401/400 这类
   确定性错误立即返回，不做无意义重试（避免把配额打光）。
"""
import logging
import time
from typing import Dict, List, Optional

import httpx

from app.config import (
    LLM_BASE_URL, LLM_API_KEY, LLM_MODEL, LLM_TIMEOUT,
    LLM_TEMPERATURE, LLM_MAX_TOKENS, LLM_USE_ENV_PROXY,
)

logger = logging.getLogger(__name__)

# 可重试的 HTTP 状态码（服务端问题 / 限流）
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}

# 查询改写用的系统提示词
REWRITE_SYSTEM_PROMPT = (
    "你是检索查询改写助手。用户会用口语提问，你需要把它改写成更适合"
    "关键词检索的形式：\n"
    "1. 把口语词替换或补充为票据/合同类文书的规范术语"
    "（例如「打车」→「客运服务费 运输服务」）。\n"
    "2. 把相对时间替换为绝对日期区间（例如「上个月」→「2026年8月」）。\n"
    "3. 只输出改写后的查询本身，用空格分隔关键词，不要解释、不要标点、不要换行。"
)


class LLMClient:
    """OpenAI 兼容协议的轻量客户端"""

    def __init__(self, base_url: Optional[str] = None,
                 api_key: Optional[str] = None,
                 model: Optional[str] = None,
                 timeout: Optional[int] = None,
                 use_env_proxy: Optional[bool] = None):
        self.base_url = (base_url or LLM_BASE_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else LLM_API_KEY
        self.model = model or LLM_MODEL
        self.timeout = timeout or LLM_TIMEOUT
        self.use_env_proxy = LLM_USE_ENV_PROXY if use_env_proxy is None else use_env_proxy

    # ---------- 状态 ----------

    @property
    def is_configured(self) -> bool:
        """是否已配置 Key。未配置时调用方应直接走降级路径，不发请求。"""
        return bool(self.api_key and self.api_key.strip())

    def get_info(self) -> Dict:
        """对外暴露的状态信息 —— 绝不包含 Key 本身"""
        return {
            "configured": self.is_configured,
            "model": self.model,
            "base_url": self.base_url,
            "use_env_proxy": self.use_env_proxy,
            "timeout": self.timeout,
        }

    # ---------- 底层调用 ----------

    def _build_client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(self.timeout, connect=10.0),
            # trust_env=False → 忽略 HTTP_PROXY / HTTPS_PROXY 等环境变量
            trust_env=self.use_env_proxy,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )

    def chat(self, messages: List[Dict[str, str]],
             temperature: Optional[float] = None,
             max_tokens: Optional[int] = None) -> Dict:
        """
        调用 /chat/completions。

        返回:
          {
            "ok": True,
            "content": "生成的文本",
            "model": "deepseek-chat",
            "usage": {...},
            "latency_ms": 1234.5,
            "error": None,
          }
          或 {"ok": False, "content": None, "error": "错误描述", ...}
        """
        if not self.is_configured:
            return {
                "ok": False, "content": None, "model": self.model,
                "usage": None, "latency_ms": 0.0,
                "error": "llm_not_configured",
            }

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": LLM_TEMPERATURE if temperature is None else temperature,
            "max_tokens": max_tokens or LLM_MAX_TOKENS,
            "stream": False,
        }

        last_error = "unknown_error"
        for attempt in range(2):  # 最多 2 次尝试（首次 + 1 次重试）
            started = time.time()
            try:
                with self._build_client() as client:
                    resp = client.post("/chat/completions", json=payload)
                latency_ms = round((time.time() - started) * 1000, 1)

                if resp.status_code == 200:
                    data = resp.json()
                    choices = data.get("choices") or []
                    content = choices[0]["message"]["content"] if choices else None
                    return {
                        "ok": True,
                        "content": content,
                        "model": data.get("model", self.model),
                        "usage": data.get("usage"),
                        "latency_ms": latency_ms,
                        "error": None,
                    }

                # 非 200：确定性错误直接返回，可重试错误走重试分支
                body = resp.text[:300]
                last_error = f"http_{resp.status_code}: {body}"
                logger.warning(f"LLM 调用返回 {resp.status_code}（第{attempt + 1}次）: {body}")
                if resp.status_code not in RETRYABLE_STATUS:
                    return {
                        "ok": False, "content": None, "model": self.model,
                        "usage": None, "latency_ms": latency_ms, "error": last_error,
                    }

            except httpx.TimeoutException as e:
                last_error = f"timeout: {e}"
                logger.warning(f"LLM 调用超时（第{attempt + 1}次）: {e}")
            except httpx.HTTPError as e:
                last_error = f"network: {e}"
                logger.warning(f"LLM 网络错误（第{attempt + 1}次）: {e}")
            except Exception as e:  # 兜底，绝不让异常逃逸
                last_error = f"unexpected: {e}"
                logger.exception("LLM 调用出现未预期错误")

        return {
            "ok": False, "content": None, "model": self.model,
            "usage": None, "latency_ms": 0.0, "error": last_error,
        }

    # ---------- 上层语义方法 ----------

    def rewrite_query(self, query: str, now_text: str = "",
                      time_label: str = "") -> Optional[str]:
        """把口语查询改写成关键词检索形式；失败返回 None"""
        hint = f"\n今天是 {now_text}。" if now_text else ""
        if time_label:
            hint += f"\n已解析出的时间范围是：{time_label}，请直接使用它。"
        resp = self.chat(
            messages=[
                {"role": "system", "content": REWRITE_SYSTEM_PROMPT},
                {"role": "user", "content": f"{query}{hint}"},
            ],
            temperature=0.0,
            max_tokens=128,
        )
        if resp["ok"] and resp["content"]:
            # 改写结果只取第一行，防止模型多嘴
            return resp["content"].strip().splitlines()[0].strip()
        return None

    def test_connection(self) -> Dict:
        """
        连通性自检：发一条最小请求验证 Key 是否有效。

        注意：会产生一次真实计费请求，仅供配置后手动验证，不要放在常规请求链路里。
        """
        if not self.is_configured:
            return {"ok": False, "error": "llm_not_configured"}
        return self.chat(
            messages=[{"role": "user", "content": "回复一个字：好"}],
            temperature=0.0,
            max_tokens=8,
        )


# ==================== 进程内单例 ====================

_client: Optional[LLMClient] = None


def get_llm_client() -> LLMClient:
    """获取全局 LLM 客户端（复用连接配置，避免每次请求重建）"""
    global _client
    if _client is None:
        _client = LLMClient()
    return _client


def reset_llm_client():
    """重置单例（.env 变更后调用，使新配置生效）"""
    global _client
    _client = None
