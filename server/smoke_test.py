# -*- coding: utf-8 -*-
"""冒烟测试：验证新模块各关键路径不崩溃。"""
import asyncio
import sys
import time
sys.path.insert(0, ".")

PASS = 0
FAIL = 0

def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} - {detail}")


async def main():
    print("=" * 60)
    print("冒烟测试：B站纯 API 爬取模块")
    print("=" * 60)

    # ── 1. config 加载 ──
    print("\n[1] config.py 配置加载")
    import config
    check("UP_UIDS 非空", len(config.UP_UIDS) == 7, f"got {len(config.UP_UIDS)}")
    check("UP_NAMES 非空", len(config.UP_NAMES) == 7)
    check("USE_PURE_API 类型正确", isinstance(config.USE_PURE_API, bool))
    check("BILI_SESSDATA 类型正确", isinstance(config.BILI_SESSDATA, str))
    check("LLM_MODEL 配置存在", config.LLM_MODEL != "")
    check("DB_PATH 存在", config.DB_PATH.exists(), str(config.DB_PATH))
    check("CRAWL_SLEEP_MIN < MAX", config.CRAWL_SLEEP_MIN < config.CRAWL_SLEEP_MAX)

    # ── 2. anti_spider 模块 ──
    print("\n[2] anti_spider.py 功能检查")
    import anti_spider
    check("BiliApiError 可实例化", anti_spider.BiliApiError(412, "test").code == 412)
    check("classify_error(-101)=cookie_expired", anti_spider.classify_error(-101) == "cookie_expired")
    check("classify_error(-352)=risk_control", "risk_control" in anti_spider.classify_error(-352))
    check("classify_error(-403)=wbi_expired", "wbi_expired" in anti_spider.classify_error(-403))
    check("classify_error(412)=risk_control", "risk_control" in anti_spider.classify_error(412))
    check("inject_dm_params 注入字段", "dm_img_list" in anti_spider.inject_dm_params({}))

    # WBI 签名（无密钥时应降级返回原 params）
    import httpx
    async with httpx.AsyncClient(timeout=5) as c:
        anti_spider.force_refresh_wbi()
        anti_spider._img_key_cache = ""
        result = await anti_spider.wbi_sign({"mid": "123"}, c)
        # 密钥拿不到时（412/超时）应返回原 dict 不崩溃
        check("wbi_sign 无密钥不崩溃", isinstance(result, dict))

    # ── 3. bili_api 模块 ──
    print("\n[3] bili_api.py 功能检查")
    import bili_api
    check("fetch_videos 可调用", callable(bili_api.fetch_videos))
    check("fetch_dynamics 可调用", callable(bili_api.fetch_dynamics))
    check("crawl_one_uid 可调用", callable(bili_api.crawl_one_uid))
    check("reset 可调用", callable(bili_api.reset))

    # 实际调用（预期：无 Cookie + 可能 IP 风控 → 应返回错误而不崩溃）
    r = await bili_api.crawl_one_uid("525121722")
    check("crawl_one_uid 返回 dict", isinstance(r, dict))
    check("返回包含 ok 字段", "ok" in r)
    check("返回包含 error 字段", "error" in r)
    check("返回包含 login_required", "login_required" in r)
    check("返回包含 videos list", isinstance(r.get("videos"), list))
    check("返回包含 dynamics list", isinstance(r.get("dynamics"), list))
    # 因为没配 Cookie，预期有错误
    if not config.BILI_SESSDATA:
        check("无 Cookie 时 error 非 None", r["error"] is not None, f"error={r['error']}")
    bili_api.reset()

    # ── 4. crawler_runner 集成 ──
    print("\n[4] crawler_runner.py 集成检查")
    import crawler_runner as cr
    check("STATE 初始化正确", "crawling" in cr.STATE and "uppers" in cr.STATE)
    check("所有 UID 在 STATE.uppers 中", all(str(u) in cr.STATE["uppers"] for u in config.UP_UIDS))
    check("is_crawling() 初始为 False", cr.is_crawling() == False)
    check("get_status_uppers() 返回列表", isinstance(cr.get_status_uppers(), list))
    check("get_status_uppers() 长度=7", len(cr.get_status_uppers()) == 7)

    # 测试 crawl_single 互斥
    cr.STATE["crawling"] = True
    result = cr.crawl_single("525121722")
    check("crawl 互斥: 已在爬取时拒绝", result.get("ok") == False)
    cr.STATE["crawling"] = False

    # ── 5. db 模块 ──
    print("\n[5] db.py 数据库检查")
    import db
    check("upsert_videos 可调用", callable(db.upsert_videos))
    check("upsert_dynamics 可调用", callable(db.upsert_dynamics))
    # 空数据插入不崩
    check("upsert_videos([]) 返回 0", db.upsert_videos([]) == 0)
    check("upsert_dynamics([]) 返回 0", db.upsert_dynamics([]) == 0)

    # ── 6. scheduler 模块 ──
    print("\n[6] scheduler.py 检查")
    import scheduler
    check("scheduler 模块可导入", True)

    # ── 总结 ──
    print("\n" + "=" * 60)
    total = PASS + FAIL
    print(f"结果: {PASS}/{total} 通过, {FAIL} 失败")
    if FAIL == 0:
        print("✅ 冒烟测试全部通过！")
    else:
        print("⚠️ 有失败项，请检查上方详情。")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
