"""Generate owner bootstrap credentials locally; never print secret values."""
import os
import secrets
import sys
from pathlib import Path

os.environ["CLOUD_MODE"] = "true"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from cryptography.fernet import Fernet
from cloud_runtime import password_hash

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / "outputs" / "cloud-admin-20261013"


def main():
    FOLDER.mkdir(parents=True, exist_ok=True)
    environment = FOLDER / ".env"
    instructions = FOLDER / "管理登录与环境变量.txt"
    if environment.exists() or instructions.exists():
        if instructions.exists():
            content = instructions.read_text("utf-8")
            content = content.replace("加密类型，Production/Preview", "Secret 类型，仅 Production；Preview 使用独立测试凭据")
            content = content.replace("模型 Key 仅在管理登录后的模型设置中填写，服务商选 DeepSeek 官方，模型 deepseek-flash，思考 disabled。", "模型 Key 只在 Vercel Production Secret 和 GitHub Actions Secret 中配置 DEEPSEEK_API_KEY；网站不提供填写或修改入口。模型 deepseek-flash，思考 disabled。")
            content = content.replace("DATABASE_URL 使用 Neon 连接串。", "DATABASE_URL 使用 worker-database.env 中的受限连接串。")
            if "provision_cloud_roles.py" not in content:
                content += "\n先用专用数据库迁移账号导入数据，再运行 tools/provision_cloud_roles.py。Vercel DATABASE_URL 使用 api-database.env 中的受限连接串；Actions 使用 worker-database.env。不要让运行服务使用数据库 owner 连接。\n"
            instructions.write_text(content, encoding="utf-8")
        from protect_local_secrets import protect
        protect()
        print("保留已有管理凭据，已更新安全配置说明并限制本地文件访问")
        return
    password = secrets.token_urlsafe(24)
    key = Fernet.generate_key().decode()
    hashed = password_hash(password)
    environment.write_text(f"ADMIN_PASSWORD_HASH={hashed}\nCREDENTIAL_ENCRYPTION_KEY={key}\n", encoding="utf-8")
    instructions.write_text(
        "仅保存在本机，不要提交到 GitHub 或发送到聊天。\n\n"
        f"网站管理登录密码：{password}\n\n"
        "Vercel → 项目 → Settings → Environment Variables，添加以下两项（Secret 类型，仅 Production；Preview 使用独立测试凭据）：\n"
        f"ADMIN_PASSWORD_HASH={hashed}\nCREDENTIAL_ENCRYPTION_KEY={key}\n\n"
        "GitHub → bili-finance-cloud → Settings → Secrets and variables → Actions：\n"
        "添加 CREDENTIAL_ENCRYPTION_KEY，值与上面相同；DATABASE_URL 使用 worker-database.env 中的受限连接串。\n"
        "先用专用数据库迁移账号导入数据，再运行 tools/provision_cloud_roles.py。Vercel DATABASE_URL 使用 api-database.env 中的受限连接串，Actions 使用 worker-database.env，不使用 owner 连接。\n"
        "保持 CLOUD_TEST_MODE=true、CLOUD_SCHEDULE_ENABLED=false，第一次测试手动运行 Cloud collection and analysis。\n"
        "模型 Key 只在 Vercel Production Secret 和 GitHub Actions Secret 中配置 DEEPSEEK_API_KEY；网站不提供填写或修改入口。模型 deepseek-flash，思考 disabled。\n",
        encoding="utf-8")
    if os.name != "nt":
        environment.chmod(0o600)
        instructions.chmod(0o600)
    from protect_local_secrets import protect
    protect()
    print("管理密码与两项环境变量已生成到本机私有文件，未打印、未上传")


if __name__ == "__main__":
    main()
