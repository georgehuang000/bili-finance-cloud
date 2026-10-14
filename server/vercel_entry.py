"""Cloud API entrypoint, independent from the separately built Vite service."""
import os

os.environ["CLOUD_MODE"] = "true"
os.environ.setdefault("CLOUD_TEST_MODE", "true")
os.environ.setdefault("CLOUD_API_ONLY", "true")
# Model/base defaults are selected centrally by config's fixed provider map.
os.environ.setdefault("LLM_THINKING", "disabled")
os.environ.setdefault("FINANCE_AUTO_ANALYSIS", "false")

from main import app
