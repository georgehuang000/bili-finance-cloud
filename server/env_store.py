# -*- coding: utf-8 -*-
"""工作区根目录 .env 的写入工具。

为什么单独抽出来：扫码登录（bili_login）和模型密钥设置（llm_settings）都要把
运行期拿到的新值写回 .env，且都必须保留文件里已有的注释、空行和其它键的顺序
—— 用户手工维护的 .env 不能被重排、被清空注释。

安全约定：本模块只做文件读写，**绝不打印值**；掩码展示由调用方负责。

与 bili_login 原实现的行为保持一致：
- 键已存在 -> 原地替换该行（该行原有的行内注释会被覆盖）
- 键不存在 -> 追加到文件末尾
- 统一 \n 行尾 + 结尾换行，UTF-8 无 BOM
"""
from __future__ import annotations

import re
from pathlib import Path

# .env 的键必须匹配这个模式；否则拒绝写入，避免用户输入把文件写坏
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def save_env(path, updates: dict) -> list:
    """把 updates 写入 .env，保留其它行。返回实际写入的键名列表（保持入参顺序）。

    值里的换行会被剔除（否则会破坏 .env 的一行一键结构）。
    """
    if not updates:
        return []

    import config
    if config.CLOUD_MODE:
        from cloud_runtime import save_settings
        return save_settings(updates)

    for key in updates:
        if not _KEY_RE.match(str(key)):
            raise ValueError(f"非法的 .env 键名: {key!r}")

    p = Path(path)
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []

    written = []
    for env_key, raw_value in updates.items():
        value = str(raw_value).replace("\r", "").replace("\n", "")
        pattern = re.compile(rf"^\s*{re.escape(env_key)}\s*=")
        for i, line in enumerate(lines):
            if pattern.match(line):
                lines[i] = f"{env_key}={value}"
                break
        else:
            lines.append(f"{env_key}={value}")
        written.append(env_key)

    # 改前先备份，写坏了还能救回来（.env 里有登录态和密钥，值得这一步）
    if p.exists():
        try:
            p.with_suffix(p.suffix + ".bak").write_text(
                p.read_text(encoding="utf-8"), encoding="utf-8"
            )
        except OSError:
            pass

    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return written


def read_env(path) -> dict:
    """把 .env 读成 dict（不插值、不展开变量）。

    只解析 ``KEY=VALUE`` 行，忽略注释与空行；用于判断某个键是否存在、
    以及做掩码展示。**调用方不要把这个 dict 整体返回给前端。**
    """
    import config
    if config.CLOUD_MODE:
        import os
        from cloud_runtime import read_settings, SECRET_KEYS
        return {**{key: os.environ[key] for key in SECRET_KEYS if key in os.environ}, **read_settings()}
    p = Path(path)
    out = {}
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, _, value = s.partition("=")
        key = key.strip()
        if _KEY_RE.match(key):
            out[key] = value.strip()
    return out
