# -*- coding: utf-8 -*-
"""LLM 客户端（OpenAI 兼容协议）的生命周期、调用参数与错误翻译。

为什么单独一层：

1. **热生效**。密钥/模型可以在网页上改（llm_settings），改完必须丢弃已经建好的
   OpenAI 客户端，否则旧密钥/旧 base_url 会被一直复用 —— 这是"改了 key 却不生效"
   这类问题的根源。
2. **思考模式**是所有调用点（日报、单视频摘要、图片识别）共用的参数，且
   DeepSeek 的思考模式**默认开启、默认 effort=high**，对"把字幕压成摘要"这种
   任务既慢又贵，必须显式关掉。
3. **错误翻译**。SDK 抛出的异常对用户毫无意义，这里统一翻译成可执行的中文提示。

安全约定：本模块**不记录也不返回 api key**；`describe_error` 只回传状态码与人话。
"""
from __future__ import annotations

import threading
import time
from urllib.parse import urlsplit

from openai import OpenAI

import config

_client = None
_lock = threading.Lock()

# 默认超时（秒）。长字幕摘要耗时较长，给足。
DEFAULT_TIMEOUT = 120.0


def build_client(api_key: str, base_url: str, timeout: float = DEFAULT_TIMEOUT) -> OpenAI:
    """按显式参数建客户端（用于"保存前先测试"，不读 config）。"""
    return OpenAI(api_key=api_key, base_url=base_url, timeout=timeout,
                  max_retries=0 if config.CLOUD_MODE and config.CLOUD_TEST_MODE else 2)


def get_client() -> OpenAI:
    """取运行中的共享客户端（按 config 当前值构建，惰性缓存）。"""
    global _client
    with _lock:
        if _client is None:
            _client = build_client(config.LLM_API_KEY, config.LLM_BASE_URL)
        return _client


def invalidate(reason: str = "") -> None:
    """丢弃缓存的客户端，使下一次调用使用新的密钥/base_url。"""
    global _client
    with _lock:
        _client = None


def is_configured() -> bool:
    return bool(config.LLM_API_KEY and config.LLM_BASE_URL)


def is_dashscope(base_url: str = None) -> bool:
    host = (urlsplit(base_url if base_url is not None else config.LLM_BASE_URL).hostname or "").lower()
    return host in ("dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com", "dashscope-us.aliyuncs.com") or host.endswith(".maas.aliyuncs.com")


def thinking_kwargs(thinking: str = None, base_url: str = None) -> dict:
    """把 LLM_THINKING 设置翻译成 OpenAI SDK 的调用参数。

    取值：
    - ``disabled``（默认）/ off / none：关闭思考，快且便宜
    - ``low`` / ``high`` / ``max``：开启思考并指定思考深度

    DeepSeek 文档：思考模式默认开启且默认 effort=high；关闭时要通过
    ``extra_body={"thinking": {"type": "disabled"}}`` 传（OpenAI SDK 不识别
    ``thinking`` 顶层参数）。
    """
    t = str(thinking if thinking is not None else config.LLM_THINKING or "disabled").strip().lower()
    if is_dashscope(base_url):
        enabled = t not in ("", "off", "none", "disabled", "false", "0")
        kwargs = {"extra_body": {"enable_thinking": enabled}}
        if enabled:
            kwargs["reasoning_effort"] = "xhigh" if t == "max" else (t if t in ("low", "high") else "low")
        return kwargs
    if t in ("", "off", "none", "disabled", "false", "0"):
        return {"extra_body": {"thinking": {"type": "disabled"}}}
    effort = t if t in ("low", "high", "max") else "low"
    return {"extra_body": {"thinking": {"type": "enabled"}}, "reasoning_effort": effort}


def thinking_enabled(thinking: str = None) -> bool:
    t = str(thinking if thinking is not None else config.LLM_THINKING or "disabled").strip().lower()
    return t in ("low", "high", "max", "on", "true", "enabled", "1")


def _exc_classes(*names):
    """按名字取 SDK 异常类（不同版本类名有增删，取不到就跳过）。"""
    import openai

    out = []
    for n in names:
        c = getattr(openai, n, None)
        if isinstance(c, type):
            out.append(c)
    return tuple(out)


def describe_error(exc: Exception) -> tuple:
    """把异常翻译成（人话提示, http状态码或None）。"""
    status = getattr(exc, "status_code", None)
    name = type(exc).__name__

    auth = _exc_classes("AuthenticationError")
    perm = _exc_classes("PermissionDeniedError")
    notfound = _exc_classes("NotFoundError")
    rate = _exc_classes("RateLimitError")
    badreq = _exc_classes("BadRequestError")
    timeout = _exc_classes("APITimeoutError", "Timeout")
    conn = _exc_classes("APIConnectionError")

    if (auth and isinstance(exc, auth)) or status == 401:
        return "API Key 无效、已被撤销，或不属于该服务商 —— 请重新粘贴", status
    if status == 402:
        return "账户余额不足，请到服务商平台充值", status
    if (perm and isinstance(exc, perm)) or status == 403:
        return "该 Key 无权访问此模型（模型名不对或未开通）", status
    if (notfound and isinstance(exc, notfound)) or status == 404:
        return "接口地址或模型名不存在 —— 检查 base_url（DeepSeek 官方应为 https://api.deepseek.com/v1）", status
    if (rate and isinstance(exc, rate)) or status == 429:
        return "触发服务商限流，请稍后重试", status
    if timeout and isinstance(exc, timeout):
        return "请求超时：服务商未在规定时间内返回", status
    if conn and isinstance(exc, conn):
        return "无法连接：base_url 写错，或本机/服务器无法访问外网", status
    if (badreq and isinstance(exc, badreq)) or status == 400:
        return "请求被拒：模型名不存在、参数不被支持，或图片放错了消息位置", status
    if status and isinstance(status, int) and status >= 500:
        return "服务商服务端错误，请稍后重试", status
    if config.CLOUD_MODE:
        return f"调用失败（{name}），请检查服务配置或稍后重试", status
    return f"调用失败（{name}）：{str(exc)[:300]}", status


def list_models(api_key: str, base_url: str, timeout: float = 20.0) -> dict:
    """向服务商拉取可用模型列表，供前端下拉选择。

    返回 {ok, models:[str], error?}。部分网关不实现 /models，此时如实回报。
    """
    if not api_key:
        return {"ok": False, "models": [], "error": "请先填写 API Key"}
    if not base_url:
        return {"ok": False, "models": [], "error": "请先填写 base_url"}
    try:
        client = build_client(api_key, base_url, timeout=timeout)
        resp = client.models.list()
        ids = []
        for m in getattr(resp, "data", []) or []:
            mid = getattr(m, "id", None)
            if mid:
                ids.append(str(mid))
        ids.sort()
        if not ids:
            return {"ok": False, "models": [], "error": "服务商返回了空的模型列表"}
        return {"ok": True, "models": ids}
    except Exception as ex:
        hint, status = describe_error(ex)
        if status == 404:
            hint = "该服务商未提供 /models 接口，请手动填写模型名"
        return {"ok": False, "models": [], "error": hint, "status": status}


def _tiny_png(color=(214, 40, 40), size=16) -> bytes:
    """最小 PNG 编码器：生成一张纯色图，用于验证 vision 能力（不引入 Pillow）。"""
    import struct
    import zlib

    w = h = size
    raw = b"".join(b"\x00" + bytes(color) * w for _ in range(h))  # 每行 filter=0 + RGB

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8bit truecolor
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def test_connection(api_key: str, base_url: str, model: str, vision: bool = False,
                    thinking: str = None, timeout: float = 60.0) -> dict:
    """用一次极小调用验证 key/base_url/模型名是否真的可用。

    返回 {ok, model, latency_ms, reply?, error?, status?}。
    vision=True 时额外发一张纯色小图，验证图片识别是否支持（DeepSeek 只有
    deepseek-flash 支持 vision，v4-pro 不支持）。
    """
    if not api_key:
        return {"ok": False, "error": "请先填写 API Key"}
    if not base_url:
        return {"ok": False, "error": "请先填写 base_url"}
    if not model:
        return {"ok": False, "error": "请先填写模型名"}

    kwargs = {"model": model, "messages": [], "timeout": timeout, "max_tokens": 256}
    if vision:
        import base64

        b64 = base64.b64encode(_tiny_png()).decode("ascii")
        kwargs["messages"] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "这张图片是什么颜色？只回答颜色名，不要解释。"},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "low"}},
                ],
            }
        ]
    else:
        kwargs["messages"] = [
            {"role": "system", "content": "你是连通性测试助手，回答尽量短。"},
            {"role": "user", "content": "请只回复两个字：正常"},
        ]

    kwargs.update(thinking_kwargs(thinking, base_url=base_url))
    # 思考模式不支持 temperature（传了也被忽略），这里统一不传，避免误解
    started = time.time()
    try:
        client = build_client(api_key, base_url, timeout=timeout)
        resp = client.chat.completions.create(**kwargs)
        latency = int((time.time() - started) * 1000)
        msg = resp.choices[0].message
        reply = (getattr(msg, "content", "") or "").strip()
        return {
            "ok": bool(reply),
            "model": model,
            "latency_ms": latency,
            "reply": reply[:120],
            "vision": vision,
            "usage": {key: getattr(getattr(resp, "usage", None), key, None) for key in ("prompt_tokens", "completion_tokens", "total_tokens")},
            **({"error": "模型未返回正文，请关闭思考模式后重试"} if not reply else {}),
        }
    except Exception as ex:
        hint, status = describe_error(ex)
        return {"ok": False, "error": hint, "status": status, "model": model}
