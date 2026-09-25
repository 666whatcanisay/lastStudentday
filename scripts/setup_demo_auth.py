"""Generate one-time demo credentials and update only demo keys in .env.

Plaintext passcodes are printed once, never saved. Run before starting the
protected application, and use --disable after closing Funnel.
"""

import argparse
import os
import secrets
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# Configuration repair must still work if an older demo configuration is bad.
os.environ["DOC_OCR_DEMO_MODE"] = "0"
from app.services.demo_auth import (  # noqa: E402
    password_hash, valid_password_hash, validated_public_origin, verify_password,
)

DEMO_KEYS = {
    "DOC_OCR_DEMO_MODE",
    "DOC_OCR_DEMO_PUBLIC_ORIGIN",
    "DOC_OCR_DEMO_GUEST_HASH",
    "DOC_OCR_DEMO_ADMIN_HASH",
}


def update_env(path: Path, new_values: dict[str, str]) -> None:
    old_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    kept = []
    for line in old_lines:
        key = line.partition("=")[0].strip()
        if key not in DEMO_KEYS and line != "# Temporary Tailscale Funnel demo access":
            kept.append(line)
    if new_values:
        if kept and kept[-1] != "":
            kept.append("")
        kept.append("# Temporary Tailscale Funnel demo access")
        kept.extend(f"{key}={value}" for key, value in new_values.items())
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".demo-tmp")
    temporary.write_text("\n".join(kept).rstrip() + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read_demo_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() in DEMO_KEYS:
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description="配置或关闭临时公网演示登录")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--origin", help="准确的 https://设备名.tailnet.ts.net")
    group.add_argument("--disable", action="store_true", help="移除全部演示认证配置")
    group.add_argument("--guest-password", metavar="PASSWORD",
                       help="只轮换访客口令，保留管理员口令；至少 12 个字符")
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env",
                        help="配置文件路径；默认项目根目录 .env")
    args = parser.parse_args()
    path = args.env_file.resolve()
    if args.disable:
        update_env(path, {})
        print("已移除演示配置。请先确认 Funnel 关闭，再重启项目使旧会话失效。")
        return 0

    if args.guest_password is not None:
        current = read_demo_values(path)
        if current.get("DOC_OCR_DEMO_MODE") != "1":
            raise SystemExit("演示模式尚未配置，请先使用 --origin 初始化")
        origin = validated_public_origin(current.get("DOC_OCR_DEMO_PUBLIC_ORIGIN", ""))
        admin_hash = current.get("DOC_OCR_DEMO_ADMIN_HASH", "")
        if not valid_password_hash(admin_hash):
            raise SystemExit("管理员口令哈希无效；未修改演示配置")
        if verify_password(args.guest_password, admin_hash):
            raise SystemExit("访客和管理员口令不能相同；未修改演示配置")
        values = {
            "DOC_OCR_DEMO_MODE": "1",
            "DOC_OCR_DEMO_PUBLIC_ORIGIN": origin,
            "DOC_OCR_DEMO_GUEST_HASH": password_hash(args.guest_password, min_length=12),
            "DOC_OCR_DEMO_ADMIN_HASH": admin_hash,
        }
        update_env(path, values)
        print("访客口令已轮换；管理员口令未变。请重启服务使旧访客会话失效。")
        return 0

    origin = validated_public_origin(args.origin)
    guest_password = secrets.token_urlsafe(18)
    admin_password = secrets.token_urlsafe(18)
    values = {
        "DOC_OCR_DEMO_MODE": "1",
        "DOC_OCR_DEMO_PUBLIC_ORIGIN": origin,
        "DOC_OCR_DEMO_GUEST_HASH": password_hash(guest_password),
        "DOC_OCR_DEMO_ADMIN_HASH": password_hash(admin_password),
    }
    update_env(path, values)
    print(f"已启用演示配置：{origin}")
    print(f"访客临时口令（只显示这一次）：{guest_password}")
    print(f"管理员口令（只显示这一次）：{admin_password}")
    print("请妥善保存口令；启动并验证演示模式后再开启 Funnel。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
