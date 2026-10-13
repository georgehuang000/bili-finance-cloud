# -*- coding: utf-8 -*-
"""图片识别（vision）：把封面图 / 动态配图的画面内容转成文字，供日报引用。

为什么要自己下载再 base64 内联，而不是把 URL 丢给模型方去抓：
B站图床对没有 Referer 的抓取可能返回 403，而模型服务商的抓取器不会带我们的
Referer；自己下载还能顺便校验类型/大小，失败点更可控。

用哪个模型：DeepSeek 官方目前只有 deepseek-flash 支持读图（deepseek-v4-pro
明确不支持），所以这里读 config.VISION_MODEL，而不是解读用的 LLM_MODEL。
"""
import base64

import httpx

import config
import llm_client

# 单图上限：B站封面通常 50~300KB，超过 8MB 基本不是正常封面，直接放弃
MAX_IMAGE_BYTES = 8 * 1024 * 1024
# 一条动态最多识别几张配图：图文贴常有 9 张，前几张已能覆盖信息，控成本
MAX_PICS_PER_ITEM = 4
ALLOWED_MIME = ("image/jpeg", "image/png", "image/gif", "image/webp")

# 让模型"只抄信息、不写作文"，输出才好直接塞进日报素材
_COVER_PROMPT = """这是一条B站财经类视频的封面图。请用中文简洁输出，不要客套、不要评价：
1) 封面上的文字：标题、板块名、个股名、数字、机构名、日期，原样抄录，不要改写；
2) 画面元素：人物、K线/分时图、图表、表格、截图等分别是什么；
3) 如果封面本身是一张研报/公告/新闻截图，把里面最关键的一两句结论抄下来。
封面没有文字就直说"无文字"。总长控制在 120 字以内。"""

_DYNAMIC_PROMPT = """这是一条B站财经 UP 主动态里的配图。请用中文简洁输出，不要客套、不要评价：
1) 图片类型（研报截图/公告截图/行情图表/数据表格/纯照片/表情包等）；
2) 图中的关键文字与数据：标题、机构名、指标名、数值、结论，原样抄录；
3) 若是行情图，说明标的与方向（涨/跌、区间）。
没有可读文字就直说"无文字"。总长控制在 120 字以内。"""


def _detail_param() -> str | None:
    """detail 档位。DeepSeek 官方支持 low/high/original/auto，low 会把长边压到
    512px，明显更省 token；其它 OpenAI 兼容网关不保证认这个字段，就不发。"""
    if config.VISION_DETAIL:
        return config.VISION_DETAIL
    if "deepseek.com" in (config.LLM_BASE_URL or ""):
        return "low"
    return None


def download_image(url: str, timeout: int = 30) -> tuple:
    """下载图片，返回 (bytes, mime)。失败抛 RuntimeError（带可读原因）。"""
    if not url:
        raise RuntimeError("图片地址为空")
    headers = {
        "User-Agent": config.USER_AGENT if hasattr(config, "USER_AGENT") else "Mozilla/5.0",
        "Referer": "https://www.bilibili.com/",
        "Accept": "image/*,*/*;q=0.8",
    }
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as c:
            r = c.get(url)
    except Exception as e:  # 网络层
        raise RuntimeError(f"下载图片失败：{type(e).__name__}") from e
    if r.status_code != 200:
        raise RuntimeError(f"下载图片失败：HTTP {r.status_code}")
    raw = r.content
    if not raw:
        raise RuntimeError("下载图片失败：内容为空")
    if len(raw) > MAX_IMAGE_BYTES:
        raise RuntimeError(f"图片过大：{len(raw) // 1024}KB")
    # 以字节头为准，不信任 URL 后缀（B站图片 URL 常带 @ 参数或 .jpg 实际是 webp）
    mime = _sniff_mime(raw)
    if mime not in ALLOWED_MIME:
        raise RuntimeError(f"不是支持的图片格式（识别为 {mime or '未知'}）")
    return raw, mime


def _sniff_mime(raw: bytes) -> str:
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _data_url(raw: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def describe(urls, kind: str = "cover", context: str = "", timeout: int = None) -> str:
    """识别一张或多张图，返回中文描述。

    kind: "cover"（视频封面）| "dynamic"（动态配图）
    context: 标题/正文，给模型一点上下文，帮助读懂封面大字
    失败抛 RuntimeError；调用方负责记录并避免反复重试。
    """
    urls = [u for u in (urls or []) if u][:MAX_PICS_PER_ITEM]
    if not urls:
        raise RuntimeError("没有可识别的图片")
    if not llm_client.is_configured():
        raise RuntimeError("未配置模型：请先在「模型设置」里填 API Key")

    # 先把图片都下下来，任何一张失败就直接失败（避免"只识别了一半"混进日报）
    parts = []
    for u in urls:
        raw, mime = download_image(u, timeout=timeout or 30)
        parts.append(_data_url(raw, mime))

    prompt = _COVER_PROMPT if kind == "cover" else _DYNAMIC_PROMPT
    if context:
        prompt += f"\n\n（内容线索，仅供理解画面参考）：{context[:200]}"

    content = [{"type": "text", "text": prompt}]
    detail = _detail_param()
    for p in parts:
        img = {"url": p}
        if detail:
            img["detail"] = detail
        content.append({"type": "image_url", "image_url": img})

    client = llm_client.get_client()
    try:
        resp = client.chat.completions.create(
            model=config.VISION_MODEL,
            messages=[{"role": "user", "content": content}],
            # 800 是防爆护栏：实测关闭思考时一次读图只用 128 个输出 token
            max_tokens=800,
            timeout=timeout or config.VISION_TIMEOUT,
            # 读图**固定关闭思考模式**，不受 LLM_THINKING 影响。
            # 实测（同一张真实封面，deepseek-flash）：
            #   thinking=disabled + 400 tokens -> 1.9s / 128 tokens / 195 字，数字全抄对
            #   thinking=max      + 400 tokens -> 正文为空（finish_reason=length，
            #                                     推理 token 把输出预算吃光）
            #   thinking=max      + 不限制     -> 69s / 18006 tokens / 正文反而更短
            # 读图是"把画面文字抄下来"的机械任务，开思考只会更慢更贵还可能拿不到结果。
            **llm_client.thinking_kwargs("disabled"),
        )
    except Exception as e:
        hint, status = llm_client.describe_error(e)
        raise RuntimeError(f"图片识别调用失败（HTTP {status}）：{hint}") from e

    try:
        choice = resp.choices[0]
        text = (choice.message.content or "").strip()
        finish = getattr(choice, "finish_reason", "") or "未知"
        reason_len = len(getattr(choice.message, "reasoning_content", "") or "")
    except (AttributeError, IndexError, TypeError):
        text, finish, reason_len = "", "未知", 0
    if not text:
        # 把 finish_reason 带出来：上次"空内容"就是靠它定位到推理吃光预算的
        raise RuntimeError(
            f"图片识别返回空内容（finish_reason={finish}，推理 {reason_len} 字）"
        )
    return text