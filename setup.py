#!/usr/bin/env python3
"""Interactive setup — walks you through every API key and writes .env.

Run this once before using agent.py:

    python setup.py

It checks what you already have, prompts only for what's missing, and writes
the result back to .env. Safe to re-run anytime.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent
ENV_PATH = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"


# Each entry: (env_key, label, required, where_to_get, hint)
KEYS = [
    (
        "AIRROI_API_KEY",
        "AirROI",
        True,
        "https://www.airroi.com/api/developer/activate",
        "Sign up, activate the developer API, copy the key.",
    ),
    (
        "FIRECRAWL_API_KEY",
        "Firecrawl",
        True,
        "https://www.firecrawl.dev",
        "Sign up → Dashboard → API Keys. Free tier works for testing.",
    ),
    (
        "ANTHROPIC_API_KEY",
        "Anthropic (Claude) — OPTIONAL, only for headless/API use",
        True,
        "https://console.anthropic.com",
        "Console → API Keys → Create Key. Pay-as-you-go, very cheap per report.",
    ),
    (
        "AIRBTICS_API_KEY",
        "Airbtics",
        False,
        "https://airbtics.com",
        "Optional market overlay. Press Enter to skip.",
    ),
    (
        "GMAIL_ADDRESS",
        "Gmail address",
        False,
        "your own Gmail account",
        "Optional — only needed to email reports. Press Enter to skip.",
    ),
    (
        "GMAIL_APP_PASSWORD",
        "Gmail App Password",
        False,
        "https://myaccount.google.com/apppasswords",
        "Optional — required ONLY if you set a Gmail address above. NOT your regular password.",
    ),
]


def read_env() -> dict[str, str]:
    """Parse the existing .env (or .env.example as fallback) into a dict."""
    source = ENV_PATH if ENV_PATH.exists() else ENV_EXAMPLE
    out: dict[str, str] = {}
    if not source.exists():
        return out
    for line in source.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        out[k.strip()] = v.strip()
    return out


def write_env(values: dict[str, str]) -> None:
    """Write values back to .env, preserving the example's comment structure."""
    if not ENV_EXAMPLE.exists():
        # Fallback: write a minimal .env
        body = "\n".join(f"{k}={v}" for k, v in values.items()) + "\n"
        ENV_PATH.write_text(body, encoding="utf-8")
        return

    out_lines: list[str] = []
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if "=" in s and not s.startswith("#"):
            key, _, _ = s.partition("=")
            key = key.strip()
            if key in values:
                out_lines.append(f"{key}={values[key]}")
                continue
        out_lines.append(line)
    ENV_PATH.write_text("\n".join(out_lines) + "\n", encoding="utf-8")


def prompt(label: str, required: bool, where: str, hint: str, current: str) -> str:
    """Prompt the user for a single key. Returns the new value (or current)."""
    print()
    print(f"── {label} {'(REQUIRED)' if required else '(optional)'} ──")
    print(f"   Where: {where}")
    print(f"   {hint}")
    if current:
        masked = current[:6] + "…" + current[-4:] if len(current) > 12 else "***"
        print(f"   Current: {masked}")
        suffix = " (Enter to keep current)"
    else:
        suffix = " (Enter to skip)" if not required else ""
    try:
        val = input(f"   Paste value{suffix}: ").strip()
    except EOFError:
        val = ""
    if not val:
        return current
    return val


def main() -> int:
    print("=" * 60)
    print("  AIRROI Comping Agent — Setup")
    print("=" * 60)
    print()
    print("This will walk you through every API key and save them to .env.")
    print("Press Enter at any prompt to keep the current value or skip.")
    print()

    existing = read_env()
    values: dict[str, str] = dict(existing)

    for env_key, label, required, where, hint in KEYS:
        current = existing.get(env_key, "")
        # Treat placeholder values as empty
        if current in {"your-airroi-api-key", "fc-...", "sk-airbtics-live-...", "sk-ant-..."}:
            current = ""
        values[env_key] = prompt(label, required, where, hint, current)

    # Make sure non-prompted defaults stay
    values.setdefault("AIRBTICS_BASE_URL", "https://crap0y5bx5.execute-api.us-east-2.amazonaws.com/prod")
    values.setdefault("OUTPUT_DIR", "./output")

    write_env(values)

    print()
    print("=" * 60)
    print("  Saved to .env")
    print("=" * 60)

    # Summary
    print()
    print("Summary:")
    missing_required: list[str] = []
    for env_key, label, required, _, _ in KEYS:
        v = values.get(env_key, "")
        if v:
            print(f"  [OK]   {label}")
        elif required:
            print(f"  [MISS] {label}  ← REQUIRED")
            missing_required.append(label)
        else:
            print(f"  [skip] {label}")

    print()
    if missing_required:
        print("Still missing required keys: " + ", ".join(missing_required))
        print("Re-run `python setup.py` once you have them.")
        return 1

    print("You're set. Try a test run:")
    print('  python agent.py --input "https://www.airbnb.com/rooms/39508095"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
