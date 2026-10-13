"""PDF text parsing in a separate, resource-limited process (no credentials)."""
import json
import sys

try:
    if sys.platform != "win32":
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (192 * 1024 * 1024, 192 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    from io import BytesIO
    from pypdf import PdfReader
    raw = sys.stdin.buffer.read(4_000_001)
    if len(raw) > 4_000_000:
        raise ValueError("PDF超过大小上限")
    reader = PdfReader(BytesIO(raw))
    if reader.is_encrypted:
        raise ValueError("不支持加密PDF")
    count = min(20, len(reader.pages))
    parts = []
    for page in reader.pages[:count]:
        text = page.extract_text() or ""
        parts.append(text)
        if sum(len(t) for t in parts) >= 24000:
            break
    result = {"text": "\n".join(parts), "note": f"PDF文字层提取；最多前{count}页，全文{len(reader.pages)}页"}
except Exception:
    result = {"error": "PDF没有可提取文字、已加密或超出解析资源限制"}
sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
