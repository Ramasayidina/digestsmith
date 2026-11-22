#!/usr/bin/env python3
"""Build the OpenMinion config DigestSmith runs with.

Why this script exists: OpenMinion's `config init` writes a large
agents.json whose defaults are tuned for interactive chat (45–90 s turn
timeouts, memory auto-extraction, self-improvement). A small local model
on CPU needs longer turns and fewer surprise LLM calls, so this script:

  1. runs `openminion config init --provider ollama` into a project-local
     OPENMINION_HOME (nothing is written to your real home directory),
  2. patches the generated agents.json: model name, generous timeouts,
     memory auto-fact-extraction OFF (it otherwise spends the first
     request of every turn on an extractor call), memory and
     self-improvement OFF (extra LLM calls a 3B model does not need).

It also tries to build a second config for the key-less Pollinations
endpoint (gpt-oss-20b) as an alternative provider; if that setup fails
it says so and the Ollama config remains the supported path.

Usage:  python configure.py [--model qwen2.5:3b]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent


def run_cli(home: Path, args: list[str], extra_env: dict | None = None) -> None:
    env = dict(os.environ, OPENMINION_HOME=str(home))
    if extra_env:
        env.update(extra_env)
    cmd = [sys.executable, "-m", "openminion", *args]
    print("+", " ".join(cmd), f"(OPENMINION_HOME={home})")
    subprocess.run(cmd, env=env, check=True, capture_output=True, text=True)


def patch_config(home: Path, model: str | None) -> Path:
    cfg_path = home / ".openminion" / "agents.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    if model and "ollama" in cfg.get("providers", {}):
        cfg["providers"]["ollama"]["model"] = model
    for block in cfg.get("providers", {}).values():
        if isinstance(block, dict) and "timeout_seconds" in block:
            block["timeout_seconds"] = max(int(block["timeout_seconds"]), 900)

    runtime = cfg.setdefault("runtime", {})
    runtime["brain_turn_timeout_seconds"] = 1500
    runtime["chat_turn_timeout_seconds"] = 1500
    runtime["memory_enabled"] = False
    runtime["demo_mode"] = False
    cfg.setdefault("gateway", {})["api_turn_timeout_seconds"] = 900
    # The memory auto-extractor otherwise consumes the first request of
    # every turn (documented OpenMinion 0.1 behaviour); a digest run has
    # nothing to remember between topics.
    cfg.setdefault("brain", {})["auto_fact_extraction"] = {"enabled": False}
    cfg.setdefault("self_improvement", {})["enabled"] = False

    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(f"Patched {cfg_path}")
    return cfg_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=os.environ.get("DIGEST_MODEL", "qwen2.5:3b"))
    parser.add_argument("--force", action="store_true", help="Regenerate configs from scratch")
    args = parser.parse_args()

    home = PROJECT_DIR / ".openminion-home"
    cfg_path = home / ".openminion" / "agents.json"
    if args.force or not cfg_path.exists():
        run_cli(home, ["config", "init", "--provider", "ollama", "--force"])
    patch_config(home, args.model)
    print(f"Ollama config ready: {cfg_path} (model: {args.model})")

    # Optional alternative: key-less Pollinations endpoint (gpt-oss-20b).
    phome = PROJECT_DIR / ".openminion-home-pollinations"
    pcfg = phome / ".openminion" / "agents.json"
    try:
        if args.force or not pcfg.exists():
            run_cli(
                phome,
                [
                    "setup", "--no-chat",
                    "--provider", "custom-openai-compatible",
                    "--base-url", "https://text.pollinations.ai/openai",
                    "--model", "openai",
                ],
                extra_env={"OPENAI_COMPATIBLE_API_KEY": "anonymous"},
            )
        patch_config(phome, None)
        print(f"Pollinations config ready: {pcfg}")
    except subprocess.CalledProcessError as exc:
        print(
            "Pollinations config could not be built (optional path):\n"
            f"{exc.stdout or ''}{exc.stderr or ''}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
