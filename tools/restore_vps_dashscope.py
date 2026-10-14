"""Restore the existing Bili Qwen credential across two authorized SSH hosts.

Secrets stay in captured memory/encrypted stdin and root-only VPS env files.
Neither the existing Pi settings nor database/login credentials are modified.
"""
import base64
import json
import os
from pathlib import Path
import subprocess


def main():
    read = r'''
import json
from dotenv import dotenv_values
from urllib.parse import urlsplit
values=dotenv_values('/opt/bili-monitor/.env')
base=values.get('LLM_BASE_URL','')
assert urlsplit(base).hostname=='dashscope.aliyuncs.com'
key=values.get('DASHSCOPE_API_KEY') or values.get('LLM_API_KEY')
assert key
print(json.dumps({'key':key,'thinking':values.get('LLM_THINKING') or 'disabled'}))
'''
    encoded = base64.b64encode(read.encode()).decode()
    command = f"sudo -n /opt/bili-monitor/server/.venv/bin/python -c 'import base64; exec(base64.b64decode(\"{encoded}\"))'"
    result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "pi-company", command], capture_output=True)
    if result.returncode:
        raise RuntimeError("Previous Bili DashScope configuration unavailable")
    values = json.loads(result.stdout)
    if values["thinking"] not in ("disabled", "low", "high", "max"):
        raise ValueError("Unsupported previous thinking configuration")
    write = r'''
import json,os,shutil,sys,tempfile
from pathlib import Path
payload=json.load(sys.stdin)
root=Path('/etc/bili-finance')
backup=root/'backups/pre-dashscope-20261014'
backup.mkdir(parents=True,exist_ok=True)
backup.chmod(0o700)
for name in ['api.env','worker.env']:
    path=root/name
    values={line.split('=',1)[0]:json.loads(line.split('=',1)[1]) for line in path.read_text().splitlines() if line}
    original={k:v for k,v in values.items() if k not in {'LLM_PROVIDER','LLM_BASE_URL','LLM_MODEL','VISION_MODEL','LLM_THINKING','DASHSCOPE_API_KEY','DEEPSEEK_API_KEY','LLM_API_KEY'}}
    saved=backup/name
    if not saved.exists(): shutil.copyfile(path,saved); saved.chmod(0o600)
    values.pop('DEEPSEEK_API_KEY',None); values.pop('LLM_API_KEY',None)
    values.update(LLM_PROVIDER='dashscope',DASHSCOPE_API_KEY=payload['key'],LLM_BASE_URL='https://dashscope.aliyuncs.com/compatible-mode/v1',LLM_MODEL='qwen3.8-max',VISION_MODEL='qwen3.8-max',LLM_THINKING=payload['thinking'])
    assert all(values[k]==v for k,v in original.items())
    with tempfile.NamedTemporaryFile(mode='w',dir=root,delete=False) as handle:
        os.chmod(handle.name,0o600)
        handle.write(''.join(k+'='+json.dumps(v)+'\n' for k,v in values.items()))
        temporary=handle.name
    os.replace(temporary,path)
print(json.dumps({'provider':'dashscope','model':'qwen3.8-max','vision_model':'qwen3.8-max','thinking':payload['thinking'],'private_env_permissions':'0600','database_and_login_credentials':'preserved'}))
'''
    encoded = base64.b64encode(write.encode()).decode()
    command = f"python3 -c 'import base64; exec(base64.b64decode(\"{encoded}\"))'"
    result = subprocess.run(["ssh", "-i", str(Path(os.environ["USERPROFILE"]) / ".ssh/id_ed25519_bili_server_82_157_178_52"),
                             "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "root@82.157.178.52", command],
                            input=json.dumps(values).encode(), capture_output=True)
    if result.returncode:
        raise RuntimeError("Private provider update failed; output suppressed")
    print(result.stdout.decode().strip())


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"DashScope restoration failed ({type(error).__name__}); no credentials printed.")
        raise SystemExit(1)
