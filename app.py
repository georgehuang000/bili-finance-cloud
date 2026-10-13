"""Vercel FastAPI entrypoint; durable work is performed by the cloud worker."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "server"))
from vercel_entry import app
