"""Cloud API entrypoint, independent from the separately built Vite service."""
import os

os.environ["CLOUD_MODE"] = "true"
os.environ.setdefault("CLOUD_TEST_MODE", "true")
os.environ.setdefault("CLOUD_API_ONLY", "true")
os.environ.setdefault("LLM_BASE_URL", "https://api.deepseek.com/v1")
os.environ.setdefault("LLM_MODEL", "deepseek-flash")
os.environ.setdefault("VISION_MODEL", "deepseek-flash")
os.environ.setdefault("LLM_THINKING", "disabled")
os.environ.setdefault("FINANCE_AUTO_ANALYSIS", "false")

from main import app
