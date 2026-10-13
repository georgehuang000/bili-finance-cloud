"""Anonymous HTTPS checks. Does not use production credentials."""
import json
import re
from urllib.parse import urlsplit
import httpx

BASE = "https://bili-finance-cloud-111-s-projects21.vercel.app"


def main():
    with httpx.Client(base_url=BASE, timeout=30, follow_redirects=False) as client:
        for path in ("/", "/history", "/research"):
            response = client.get(path)
            assert response.status_code == 200, f"Page failed: {path}, status={response.status_code}"
            assert "text/html" in response.headers.get("content-type", "")
            assert response.headers.get("x-frame-options") == "DENY"
            assert "default-src 'self'" in response.headers.get("content-security-policy", "")
        html = client.get("/").text
        script = re.search(r'<script[^>]+src="([^"]+)"', html).group(1)
        assert not urlsplit(script).netloc  # Same-origin production asset only.
        asset = client.get(script)
        assert asset.status_code == 200 and "javascript" in asset.headers.get("content-type", "")
        assert "/api/llm/settings" not in asset.text and "模型设置" not in asset.text
        health = client.get("/api/health")
        assert health.status_code == 200 and health.json()["ok"] and health.json()["cloud"]
        assert client.get("/api/auth/session").json()["can_manage"] is False
        authors = client.get("/api/uppers").json()
        assert len(authors) == 7
        for path in ("/api/uppers/search?q=fixture", "/api/llm/status", "/api/jobs", "/api/login/status"):
            assert client.get(path).status_code == 401, f"Private route exposed: {path}"
        for path in ("/api/crawl", "/api/uppers", "/api/llm/test", "/api/login/qrcode"):
            assert client.post(path, json={}).status_code == 401, f"Anonymous write exposed: {path}"
        assert client.get("/.env").status_code == 404
        print(json.dumps({"public_pages": "passed", "api_database": "passed", "anonymous_management_denied": "passed", "authors": len(authors), "model_key_ui": "absent"}))


if __name__ == "__main__":
    main()
