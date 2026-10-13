"""Bounded retrieval of public disclosure links and source-grounded checking."""
import hashlib
import ipaddress
import json
import socket
import subprocess
import sys
import threading
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import BaseModel, Field
from typing import Literal

import finance_analysis as analysis

# Official exchanges, public filings and issuer domains supported in this release.
DOMAINS = ("sec.gov", "hkexnews.hk", "cninfo.com.cn", "sse.com.cn", "szse.cn",
           "nvidia.com", "tsmc.com", "smics.com", "tencent.com", "alibabagroup.com",
           "microsoft.com", "alphabet.com", "amazon.com", "apple.com", "amd.com")
_lock = threading.Lock()
_busy = False


class Verification(BaseModel):
    status: Literal["提供支持", "提供反证", "信息不足"]
    summary: str = Field(min_length=1, max_length=1200)
    quote: str = Field(default="", max_length=800)
    period: str = Field(default="未明确", max_length=200)
    limitations: str = Field(default="", max_length=1200)


def init_store():
    with analysis.connect() as c:
        c.execute("CREATE TABLE IF NOT EXISTS finance_evidence_jobs(id INTEGER PRIMARY KEY,claim_id INTEGER,url TEXT,status TEXT,message TEXT,started_ts INTEGER,finished_ts INTEGER)")
        if not analysis.config.CLOUD_MODE:
            c.execute("UPDATE finance_evidence_jobs SET status='interrupted',message='服务重启，核验未完成' WHERE status='running'")


def validate_url(url):
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("请使用不含登录信息的 HTTPS 公告或财报链接")
    if not any(host == domain or host.endswith("." + domain) for domain in DOMAINS):
        raise ValueError("当前支持交易所、巨潮、SEC及主要公司官网披露链接；该域名尚未支持")
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("仅支持公开互联网披露地址")
    return host


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.skip = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip += 1
        elif not self.skip and tag in ("p", "div", "br", "tr", "h1", "h2", "li"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def fetch_document(url):
    with httpx.Client(timeout=25, follow_redirects=False, trust_env=False) as client:
        for _ in range(5):
            validate_url(url)
            with client.stream("GET", url, headers={"User-Agent": "BiliResearch/1.0 public-disclosure-reader", "Accept": "text/html,application/pdf,text/plain"}) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers.get("location", ""))
                    continue
                response.raise_for_status()
                content = bytearray()
                for part in response.iter_bytes():
                    content.extend(part)
                    if len(content) > 4_000_000:
                        raise ValueError("文件超过4MB，请选用较短的公告或财报摘要")
                mime = response.headers.get("content-type", "").lower()
                if bytes(content).startswith(b"%PDF"):
                    process = subprocess.run([sys.executable, str(Path(__file__).with_name("finance_pdf_worker.py"))], input=bytes(content), capture_output=True, timeout=25)
                    if process.returncode:
                        raise ValueError("PDF提取失败或超出解析资源限制")
                    result = json.loads(process.stdout)
                    if result.get("error"):
                        raise ValueError(result["error"])
                    text = result["text"]
                    note = result["note"]
                elif "html" in mime or "text/plain" in mime:
                    decoded = bytes(content).decode(response.encoding or "utf-8", errors="replace")
                    parser = PlainText()
                    parser.feed(decoded)
                    text = "".join(parser.parts)
                    note = "网页正文提取；表格布局可能丢失"
                else:
                    raise ValueError("当前仅支持网页正文和有文字层的PDF")
                if len(text.strip()) < 80:
                    raise ValueError("未取得足够正文，可能需要登录或为扫描件")
                if len(text) > 24000:
                    note += "；正文超过上限，仅核验前24000字"
                return text[:24000], url, note
    raise ValueError("披露链接跳转次数过多")


def verify(claim_id, url):
    claim = analysis.get_claim(claim_id)
    if not claim:
        raise ValueError("观点不存在")
    text, final_url, note = fetch_document(url)
    digest = hashlib.sha256(text.encode()).hexdigest()
    with analysis.connect() as c:
        cached = c.execute("SELECT id FROM finance_evidence WHERE claim_id=? AND url=? AND text_hash=? AND model=?", (claim_id, final_url, digest, analysis.config.LLM_MODEL)).fetchone()
    if cached:
        return cached["id"]
    result, model = analysis.call_json((analysis.SKILLS / "证据核验.md").read_text("utf-8"), {
        "claim": {k: claim.get(k) for k in ("subject", "claim", "horizon", "quote", "pub_ts")},
        "document": {"url": final_url, "source_label": "用户补充的官网或披露平台资料", "retrieved_on": time.strftime("%Y-%m-%d"), "text": text, "limits": note},
        "schema": Verification.model_json_schema(),
    })
    payload = Verification.model_validate(result).model_dump()
    if payload["quote"]:
        start, end = analysis.locate_quote(text, payload["quote"])
        payload["quote"] = text[start:end]
    elif payload["status"] != "信息不足":
        raise ValueError("模型未提供可定位证据，核验结果未保存")
    payload["extraction_note"] = note
    payload["checked_ts"] = int(time.time())
    payload["historical_warning"] = "此核验使用当前补充资料；请核对披露日期后再用于历史判断"
    with analysis.connect() as c:
        return c.execute("INSERT INTO finance_evidence(claim_id,url,text_hash,model,created_ts,source_label,data) VALUES(?,?,?,?,?,?,?)", (claim_id, final_url, digest, model, int(time.time()), "官网或披露平台资料", json.dumps(payload, ensure_ascii=False))).lastrowid


def start_bg(claim_id, url):
    global _busy
    # Validate scheme/host now, resolving and retrieving only in the worker.
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None,443) or not any((parsed.hostname or "").lower().rstrip(".") == d or (parsed.hostname or "").lower().rstrip(".").endswith("." + d) for d in DOMAINS):
        raise ValueError("请填写支持的官方HTTPS披露链接")
    if not analysis.get_claim(claim_id):
        raise ValueError("观点不存在")
    with _lock:
        if _busy:
            return None
        _busy = True
    try:
        with analysis.connect() as c:
            job_id = c.execute("INSERT INTO finance_evidence_jobs(claim_id,url,status,started_ts) VALUES(?,?,'running',?)", (claim_id, url, int(time.time()))).lastrowid
    except Exception:
        with _lock:
            _busy = False
        raise
    def worker():
        global _busy
        try:
            verify(claim_id, url)
            status, message = "complete", "核验完成"
        except Exception as ex:
            status = "failed"
            if isinstance(ex, httpx.HTTPStatusError):
                message = f"披露网站返回HTTP {ex.response.status_code}，未读取到正文；请使用可访问的原始披露网页或PDF"
            else:
                message = str(ex)[:250] if isinstance(ex, ValueError) else "资料获取或模型核验失败，请稍后重试"
        finally:
            try:
                with analysis.connect() as c:
                    c.execute("UPDATE finance_evidence_jobs SET status=?,message=?,finished_ts=? WHERE id=?", (status, message, int(time.time()), job_id))
            finally:
                with _lock:
                    _busy = False
    threading.Thread(target=worker, daemon=True, name="finance-evidence").start()
    return job_id


def jobs(uid=None):
    with analysis.connect() as c:
        query = "SELECT j.* FROM finance_evidence_jobs j JOIN finance_claims f ON f.id=j.claim_id"
        return [dict(r) for r in c.execute(query + (" WHERE f.uid=?" if uid else "") + " ORDER BY j.id DESC LIMIT 10", (uid,) if uid else ())]
