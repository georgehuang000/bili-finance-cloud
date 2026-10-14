"""Provider selection must keep secrets bound to fixed official destinations."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


class ProviderTests(unittest.TestCase):
    def configure(self, provider):
        env = dict(os.environ, CLOUD_MODE="true", LLM_PROVIDER=provider,
                   DASHSCOPE_API_KEY="fixture-qwen-key", DEEPSEEK_API_KEY="fixture-deepseek-key",
                   LLM_BASE_URL="https://attacker.example/v1")
        for name in ("LLM_MODEL", "VISION_MODEL", "LLM_API_KEY", "VERCEL"):
            env.pop(name, None)
        code = "import config,json,os; print(json.dumps({'base':config.LLM_BASE_URL,'model':config.LLM_MODEL,'vision':config.VISION_MODEL,'uses_dashscope_key':config.LLM_API_KEY==os.environ['DASHSCOPE_API_KEY'],'uses_deepseek_key':config.LLM_API_KEY==os.environ['DEEPSEEK_API_KEY']}))"
        return subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parent,
                              env=env, capture_output=True, text=True)

    def test_dashscope_key_model_and_fixed_destination(self):
        result = self.configure("dashscope")
        self.assertEqual(result.returncode, 0)
        values = json.loads(result.stdout)
        self.assertEqual(values["base"], "https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.assertEqual(values["model"], "qwen3.8-max")
        self.assertEqual(values["vision"], "qwen3.8-max")
        self.assertTrue(values["uses_dashscope_key"])
        self.assertFalse(values["uses_deepseek_key"])

    def test_existing_deepseek_configuration_stays_bound(self):
        result = self.configure("deepseek")
        self.assertEqual(result.returncode, 0)
        values = json.loads(result.stdout)
        self.assertEqual(values["base"], "https://api.deepseek.com/v1")
        self.assertEqual(values["model"], "deepseek-flash")
        self.assertTrue(values["uses_deepseek_key"])
        self.assertFalse(values["uses_dashscope_key"])

    def test_unknown_cloud_provider_fails_closed(self):
        result = self.configure("custom")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("fixture-qwen-key", result.stderr)
        self.assertNotIn("fixture-deepseek-key", result.stderr)


if __name__ == "__main__":
    unittest.main()
