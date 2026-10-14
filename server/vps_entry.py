"""Public VPS API: enforce the same owner boundary as the cloud deployment."""
import os

os.environ["CLOUD_MODE"] = "true"
os.environ["CLOUD_API_ONLY"] = "true"

from main import app
