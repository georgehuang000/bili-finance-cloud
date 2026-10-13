# -*- coding: utf-8 -*-
"""Standalone Bilibili creator-dynamics fetcher.

WHY THIS EXISTS
---------------
MediaCrawler fetches the space-dynamics feed
(/x/polymer/web-dynamic/v1/feed/space) with an out-of-browser ``httpx``
client (see media_platform/bilibili/client.py::request). It only copies the
cookie string plus a static header set (Referer/Origin =
https://www.bilibili.com) and WBI-signs three params (offset/host_mid/
platform). Bilibili has hardened this specific endpoint with a
browser-environment check (real TLS/JA3 fingerprint, Referer pointing at the
space page, and the browser-only anti-crawl params dm_img_list / dm_img_str /
dm_cover_img_str / web_location). An httpx request satisfies none of these, so
the endpoint answers 412 Precondition Failed every time -- while the more
lenient video endpoints keep working. The same request issued from inside a
logged-in browser page context returns HTTP 200 / code 0.

STRATEGY
--------
Run a Playwright worker under MediaCrawler's own virtualenv (which already has
Playwright + system Chrome and the persisted bili login profile). The worker
opens the real space/dynamic page and pulls the feed *from inside the page
context* via ``page.evaluate`` fetch (credentials:'include') AND by capturing
the page's own feed/space responses. Both paths inherit the real browser
fingerprint / cookies / Referer, so no 412.

This module has two roles:

* PARENT (imported by crawler_runner, runs under server/.venv): ``fetch_dynamics``
  spawns the worker as a subprocess using ``MC_PYTHON`` and reads back a JSON
  result file. This keeps the heavy Playwright dependency isolated in the
  MediaCrawler venv (no need to install Playwright into server/.venv).
* WORKER (``python dynamics_fetcher.py --worker ...`` run by MC_PYTHON): does
  the actual Playwright work. It imports Playwright lazily and never imports the
  server ``config``/``db`` modules, so it is safe to run from the MediaCrawler
  working directory.
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

FEED_URL_MATCH = "web-dynamic/v1/feed/space"
API_BASE = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"


# ===========================================================================
# WORKER SIDE (executed by MediaCrawler .venv python)
# ===========================================================================
def _strip_type(dtype: str) -> str:
    dtype = dtype or ""
    prefix = "DYNAMIC_TYPE_"
    return dtype[len(prefix):] if dtype.startswith(prefix) else dtype


def _text_from_block(block) -> str:
    """Extract text from a desc/summary block that may expose either a flat
    ``text`` field or a ``rich_text_nodes`` list (opus / itemOpusStyle style)."""
    if not isinstance(block, dict):
        return ""
    t = block.get("text")
    if t:
        return t
    nodes = block.get("rich_text_nodes")
    if isinstance(nodes, list) and nodes:
        parts = [n.get("text", "") for n in nodes if isinstance(n, dict)]
        joined = "".join(parts).strip()
        if joined:
            return joined
    return ""


def _extract_text(module_dynamic: dict) -> str:
    """Best-effort text extraction from a dynamic's module_dynamic block.

    Covers plain text (desc.text), rich-text nodes, opus/itemOpusStyle picture
    posts (major.opus.summary), video dynamics (major.archive.title) and
    articles (major.article.title).
    """
    if not isinstance(module_dynamic, dict):
        return ""
    # 1) top-level desc (plain text posts + most DRAW captions)
    t = _text_from_block(module_dynamic.get("desc"))
    if t:
        return t
    major = module_dynamic.get("major") or {}
    if isinstance(major, dict):
        # 2) opus style (itemOpusStyle wraps DRAW/article text here)
        opus = major.get("opus") or {}
        if isinstance(opus, dict):
            t = _text_from_block(opus.get("summary"))
            if t:
                return t
            if opus.get("title"):
                return opus.get("title")
        # 3) video dynamic -> archive title
        arch = major.get("archive") or {}
        if isinstance(arch, dict) and arch.get("title"):
            return arch.get("title")
        # 4) article dynamic
        article = major.get("article") or {}
        if isinstance(article, dict) and article.get("title"):
            return article.get("title")
    return ""


def _parse_item(it: dict, uid: str):
    if not isinstance(it, dict):
        return None
    dyn_id = str(it.get("id_str") or "").strip()
    if not dyn_id:
        return None
    dtype = _strip_type(it.get("type", ""))
    modules = it.get("modules") or {}
    module_dynamic = modules.get("module_dynamic") or {}
    text = _extract_text(module_dynamic)
    # forwarded dynamics: prepend own text (may be empty) - keep own text only,
    # original content already lives in its own dynamic row.
    author = modules.get("module_author") or {}
    pub_ts = 0
    try:
        pub_ts = int(author.get("pub_ts") or 0)
    except Exception:
        pub_ts = 0
    return {
        "dyn_id": dyn_id,
        "uid": str(uid),
        "type": dtype,
        "text": text or "",
        "pub_ts": pub_ts,
        "url": f"https://t.bilibili.com/{dyn_id}",
    }


_FETCH_JS = r"""
async ([uid, offset]) => {
  const params = new URLSearchParams({
    offset: offset || '',
    host_mid: String(uid),
    timezone_offset: '-480',
    platform: 'web',
    features: 'itemOpusStyle,opusBigCover,onlyfansVote,endFooterHidden,decorationCard,onlyfansAssetsV2,forwardListHidden,ugcDelete,onlyfansQaCard,commentsNewVersion',
    web_location: '333.1387'
  });
  const url = 'https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space?' + params.toString();
  try {
    const resp = await fetch(url, {
      credentials: 'include',
      headers: {'accept': 'application/json, text/plain, */*'}
    });
    const status = resp.status;
    let data = null;
    try { data = await resp.json(); } catch (e) { data = {_parse_error: String(e)}; }
    return {status, data};
  } catch (e) {
    return {status: -1, data: {_fetch_error: String(e)}};
  }
}
"""


def _prepare_profile_copy(src_dir: str) -> str:
    """Copy just the login-critical files from the persisted Chrome profile into
    a fresh unique temp user-data-dir.

    Launching Chrome directly on the shared MediaCrawler profile fails with
    exitCode=21 ("profile already in use") whenever any other Chrome instance
    (a MediaCrawler run or a leftover process) still holds it. Cloning the
    cookies + Local State (the DPAPI-wrapped os_crypt key) into a private dir
    sidesteps the SingletonLock entirely while preserving the login session.
    """
    src = Path(src_dir)
    dst = Path(tempfile.mkdtemp(prefix="biliprof_"))
    (dst / "Default" / "Network").mkdir(parents=True, exist_ok=True)
    # top-level files
    for name in ("Local State", "First Run"):
        s = src / name
        if s.exists():
            try:
                shutil.copy2(s, dst / name)
            except Exception:
                pass
    default_src = src / "Default"
    default_dst = dst / "Default"
    for rel in (
        "Network/Cookies",
        "Network/Cookies-journal",
        "Network/Network Persistent State",
        "Cookies",
        "Cookies-journal",
        "Preferences",
        "Secure Preferences",
    ):
        s = default_src / rel
        if s.exists():
            d = default_dst / rel
            d.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(s, d)
            except Exception:
                pass
    # Local Storage (wbi keys etc.) - best effort
    ls_src = default_src / "Local Storage"
    if ls_src.exists():
        try:
            shutil.copytree(ls_src, default_dst / "Local Storage")
        except Exception:
            pass
    return str(dst)


def _worker_main(uid, out_path, user_data_dir, max_items, pages):
    from playwright.sync_api import sync_playwright

    result = {
        "ok": False,
        "uid": str(uid),
        "items": [],
        "fetch_code": None,
        "fetch_status": None,
        "captured_pages": 0,
        "message": "",
        "error": None,
    }
    seen = {}
    order = []

    def _add_items(items):
        for it in items or []:
            row = _parse_item(it, uid)
            if row and row["dyn_id"] not in seen:
                seen[row["dyn_id"]] = row
                order.append(row["dyn_id"])

    captured = []

    def _on_response(resp):
        try:
            if FEED_URL_MATCH in resp.url:
                data = resp.json()
                captured.append(data)
        except Exception:
            pass

    # Clone the login profile into a private temp dir to avoid the shared-profile
    # SingletonLock (Chrome exitCode=21).
    work_profile = None
    try:
        work_profile = _prepare_profile_copy(user_data_dir)
    except Exception as ex:
        result["message"] += f"profile_copy_warn:{ex}; "
        work_profile = str(user_data_dir)

    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=work_profile,
                headless=True,
                channel="chrome",
                accept_downloads=True,
                viewport={"width": 1920, "height": 1080},
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(45000)
            page.on("response", _on_response)

            # Open the real space/dynamic page: this both establishes the correct
            # Referer / page context for evaluate-fetch AND triggers the page's
            # own (correctly-signed) feed/space request that we capture.
            try:
                page.goto(
                    f"https://space.bilibili.com/{uid}/dynamic",
                    wait_until="domcontentloaded",
                    timeout=45000,
                )
            except Exception as ex:
                result["message"] += f"goto_warn:{ex}; "
            # give the page a moment to fire its own feed request
            try:
                page.wait_for_timeout(3500)
            except Exception:
                pass

            # ---- primary path: page-context fetch, paginated ----
            offset = ""
            first_code = None
            first_status = None
            for i in range(max(1, int(pages))):
                try:
                    res = page.evaluate(_FETCH_JS, [str(uid), offset])
                except Exception as ex:
                    result["message"] += f"evaluate_err:{ex}; "
                    break
                status = res.get("status")
                data = res.get("data") or {}
                code = data.get("code")
                if i == 0:
                    first_code = code
                    first_status = status
                if status != 200 or code != 0:
                    result["message"] += f"fetch_p{i}_status={status}_code={code}; "
                    break
                payload = data.get("data") or {}
                _add_items(payload.get("items"))
                has_more = payload.get("has_more")
                offset = payload.get("offset") or ""
                if not has_more or not offset or len(order) >= max_items:
                    break
                page.wait_for_timeout(1200)
            result["fetch_code"] = first_code
            result["fetch_status"] = first_status

            # ---- fallback / supplement: scroll to trigger natural requests ----
            if len(order) < max_items:
                for _ in range(max(1, int(pages))):
                    try:
                        page.mouse.wheel(0, 3200)
                        page.wait_for_timeout(1500)
                    except Exception:
                        break
                    if len(order) >= max_items:
                        break

            # merge captured responses (the page's own signed requests)
            for data in captured:
                try:
                    payload = (data or {}).get("data") or {}
                    _add_items(payload.get("items"))
                except Exception:
                    pass
            result["captured_pages"] = len(captured)

            try:
                context.close()
            except Exception:
                pass

        items = [seen[d] for d in order][:max_items]
        result["items"] = items
        result["ok"] = len(items) > 0
        if not result["ok"] and not result["error"]:
            # surface a risk-control note if the captured/fetch code indicates it
            if result["fetch_code"] in (-352, -401, -509) or result["fetch_status"] == 412:
                result["error"] = f"risk_control(code={result['fetch_code']},http={result['fetch_status']})"
            else:
                result["error"] = "no_data"
    except Exception as ex:
        result["error"] = f"worker_exception:{ex}"
    finally:
        # remove the temp profile clone
        if work_profile and work_profile != str(user_data_dir):
            shutil.rmtree(work_profile, ignore_errors=True)

    Path(out_path).write_text(
        json.dumps(result, ensure_ascii=False), encoding="utf-8"
    )
    return 0 if result["ok"] else 2


# ===========================================================================
# PARENT SIDE (imported by crawler_runner, runs under server/.venv)
# ===========================================================================
def fetch_dynamics(uid: str, max_items: int = 30, pages: int = 3, timeout: int = 150) -> dict:
    """Fetch a creator's recent dynamics via the Playwright worker subprocess.

    Returns dict: {ok, count, items, error, code, http_status, raw_message}.
    ``items`` are rows ready for db.upsert_dynamics.
    """
    import config  # server config (only available in server venv)

    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    out = config.TMP_DIR / f"{uid}_dynfetch.json"
    try:
        if out.exists():
            out.unlink()
    except Exception:
        pass

    cmd = [
        str(config.MC_PYTHON),
        str(Path(__file__).resolve()),
        "--worker",
        "--uid", str(uid),
        "--out", str(out),
        "--user-data-dir", str(config.MC_LOGIN_DATA_DIR),
        "--max", str(max_items),
        "--pages", str(pages),
    ]
    error = None
    combined = ""
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(config.MEDIACRAWLER_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            combined = (stdout or "") + "\n" + (stderr or "")
        except subprocess.TimeoutExpired:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
            )
            try:
                proc.communicate(timeout=15)
            except Exception:
                pass
            error = "timeout"
    except Exception as ex:
        error = f"spawn_error:{ex}"

    result = None
    if out.exists():
        try:
            result = json.loads(out.read_text(encoding="utf-8"))
        except Exception as ex:
            error = error or f"result_parse_error:{ex}"

    if result is None:
        return {
            "ok": False,
            "count": 0,
            "items": [],
            "error": error or "no_result_file",
            "code": None,
            "http_status": None,
            "raw_message": combined[-500:],
        }

    items = result.get("items") or []
    return {
        "ok": bool(result.get("ok")) and len(items) > 0,
        "count": len(items),
        "items": items,
        "error": error or result.get("error"),
        "code": result.get("fetch_code"),
        "http_status": result.get("fetch_status"),
        "raw_message": result.get("message", ""),
    }


def _build_arg_parser():
    ap = argparse.ArgumentParser(description="Bilibili dynamics fetcher")
    ap.add_argument("--worker", action="store_true", help="run the playwright worker")
    ap.add_argument("--uid", required=True)
    ap.add_argument("--out", help="worker output json path")
    ap.add_argument("--user-data-dir", help="persistent chrome profile dir")
    ap.add_argument("--max", type=int, default=30)
    ap.add_argument("--pages", type=int, default=3)
    return ap


if __name__ == "__main__":
    args = _build_arg_parser().parse_args()
    if args.worker:
        rc = _worker_main(
            uid=args.uid,
            out_path=args.out,
            user_data_dir=args.user_data_dir,
            max_items=args.max,
            pages=args.pages,
        )
        sys.exit(rc)
    else:
        # Convenience: run parent path directly for manual testing.
        r = fetch_dynamics(args.uid, max_items=args.max, pages=args.pages)
        print(json.dumps(r, ensure_ascii=False, indent=2))
