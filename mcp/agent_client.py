#!/usr/bin/env python3
"""CLI entry point for the local coding agent (thin wrapper over agent_core).

Usage:
  OLLAMA_HOST=http://localhost:11434 python agent_client.py "Write fib.py and run it."
"""
import asyncio
import sys

from agent_core import run_agent


def main() -> None:
    if len(sys.argv) < 2:
        print('Usage: python agent_client.py "your task here"')
        raise SystemExit(2)
    result = asyncio.run(run_agent(" ".join(sys.argv[1:]), log=print))
    print("\n=== FINAL ===")
    print(result["final"])
    print(f"[stop_reason={result.get('stop_reason')} "
          f"steps={len(result.get('steps', []))} "
          f"elapsed={result.get('elapsed')}s]")


if __name__ == "__main__":
    main()
