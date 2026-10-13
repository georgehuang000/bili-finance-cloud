"""Restrict this project's credential files and migration backups locally."""
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def protect(extra_paths=()):
    paths = [ROOT / ".env", ROOT / ".env.bak", ROOT / "server" / "data.db"]
    paths += [ROOT / "outputs" / name for name in ("cloud-admin-20261013", "cloud-data-20261013")]
    paths += [Path(path) for path in extra_paths]
    paths = [path.resolve() for path in paths if path.exists()]
    if any(not path.is_relative_to(ROOT.resolve()) for path in paths):
        raise ValueError("Credential path is outside the workspace")
    if os.name == "nt":
        identity = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, check=True).stdout.decode(errors="replace")
        sid = re.search(r"S-1-5-(?:[0-9]+-)*[0-9]+", identity).group()
        for path in paths:
            rights = "(OI)(CI)F" if path.is_dir() else "F"
            subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:{rights}", f"*S-1-5-18:{rights}", f"*S-1-5-32-544:{rights}"], capture_output=True, check=True)
    else:
        for path in paths:
            path.chmod(0o700 if path.is_dir() else 0o600)
            if path.is_dir():
                for file in path.iterdir():
                    if file.is_file():
                        file.chmod(0o600)
    return len(paths)


if __name__ == "__main__":
    print(f"Protected {protect()} credential/backup paths; no secret contents read or printed.")
