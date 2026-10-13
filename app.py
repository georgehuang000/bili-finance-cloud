"""Vercel FastAPI entrypoint; durable work is performed by the cloud worker."""
import os
import sys
from pathlib import Path

os.environ.setdefault("CLOUD_MODE", "true")
os.environ.setdefault("CLOUD_TEST_MODE", "true")
os.environ.setdefault("LLM_BASE_URL", "https://api.deepseek.com/v1")
os.environ.setdefault("LLM_MODEL", "deepseek-flash")
os.environ.setdefault("VISION_MODEL", "deepseek-flash")
os.environ.setdefault("LLM_THINKING", "disabled")
os.environ.setdefault("FINANCE_AUTO_ANALYSIS", "false")
sys.path.insert(0, str(Path(__file__).resolve().parent / "server"))
from main import app
