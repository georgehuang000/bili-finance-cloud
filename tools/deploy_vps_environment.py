"""Transfer existing Bili deployment secrets to its explicitly authorized VPS.

The SSH command contains only writer code. Secrets travel through encrypted
stdin, never command arguments or tool output. Existing keys are not rotated.
"""
import argparse
import base64
import json
import subprocess
from pathlib import Path

from dotenv import dotenv_values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--identity", type=Path, required=True)
    args = parser.parse_args()
    if args.host != "root@82.157.178.52":
        raise ValueError("This transfer is restricted to the user-authorized server")
    root = Path(__file__).resolve().parents[1]
    private = root / "outputs/cloud-admin-20261013"
    shared = dotenv_values(private / ".env")
    local = dotenv_values(root / ".env")
    key = local.get("DEEPSEEK_API_KEY") or local.get("LLM_API_KEY") or local.get("deepseek_key")
    if not key:
        raise ValueError("Existing official model credential is unavailable")
    common = {"CLOUD_MODE": "true", "CLOUD_API_ONLY": "true", "CLOUD_TEST_MODE": "false",
              "CREDENTIAL_ENCRYPTION_KEY": shared["CREDENTIAL_ENCRYPTION_KEY"], "DEEPSEEK_API_KEY": key,
              "LLM_BASE_URL": "https://api.deepseek.com/v1", "LLM_MODEL": "deepseek-flash",
              "LLM_THINKING": "disabled", "VISION_MODEL": "deepseek-flash", "FINANCE_AUTO_ANALYSIS": "false",
              "USE_PURE_API": "true", "AUTO_CRAWL_ON_START": "false"}
    api = {**common, "DATABASE_URL": dotenv_values(private / "api-database.env")["DATABASE_URL"],
           "ADMIN_PASSWORD_HASH": shared["ADMIN_PASSWORD_HASH"]}
    worker = {**common, "DATABASE_URL": dotenv_values(private / "worker-database.env")["DATABASE_URL"],
              "CLOUD_WORKER": "true", "CLOUD_SCHEDULE_ENABLED": "true"}
    writer = r'''
import json, os, re, sys, tempfile
from pathlib import Path
data = json.load(sys.stdin)
root = Path('/etc/bili-finance')
assert root.is_dir() and set(data) == {'api.env', 'worker.env'}
for name, values in data.items():
    assert all(re.fullmatch(r'[A-Z0-9_]+', key) and isinstance(value, str) and value.isascii() and not any(c in value for c in '\r\n\0') for key, value in values.items())
    content = ''.join(key + '=' + json.dumps(value) + '\n' for key, value in values.items())
    path = root / name
    if path.exists():
        assert path.read_text() == content, 'Existing credentials differ; no rotation performed'
        continue
    with tempfile.NamedTemporaryFile(mode='w', dir=root, delete=False) as handle:
        os.chmod(handle.name, 0o600)
        handle.write(content)
        temporary = handle.name
    os.replace(temporary, path)
print('Existing API/worker credentials installed with 0600 permissions; no values printed.')
'''
    encoded = base64.b64encode(writer.encode()).decode()
    command = f"python3 -c 'import base64; exec(base64.b64decode(\"{encoded}\"))'"
    result = subprocess.run(["ssh", "-i", str(args.identity), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", args.host, command],
                            input=json.dumps({"api.env": api, "worker.env": worker}).encode(), capture_output=True)
    if result.returncode:
        raise RuntimeError("Secure environment transfer failed; credential payload and remote output suppressed")
    print(result.stdout.decode().strip())


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"VPS environment transfer failed ({type(error).__name__}); no credentials printed.")
        raise SystemExit(1)
