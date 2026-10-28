"""CLI: python -m agent "open Battery settings and tell me the battery level" [--model muse-spark-1.3]"""
import argparse
import os
import time

from agent.device import Device
from agent.graph import run
from agent.model import MetaClient, load_env


def main() -> None:
    p = argparse.ArgumentParser(prog="agent")
    p.add_argument("goal")
    p.add_argument("--model", default="muse-wonder-1.0")
    p.add_argument("--effort", default="minimal")
    p.add_argument("--max-steps", type=int, default=15)
    p.add_argument("--serial")
    a = p.parse_args()
    load_env()
    device = Device(a.serial)
    if os.environ.get("PHONE_PROFILE", "light") == "light" and device.is_emulator():
        device.prepare()
    model = MetaClient(a.model, a.effort)
    trace = f"runs/{time.strftime('%Y%m%d-%H%M%S')}"
    res = run(a.goal, device, model, a.max_steps, trace,
              on_step=lambda n, act, why: print(f"[{n}] {act}  # {why}"))
    print(f"\n{res.status.upper()} after {res.steps} step(s): {res.answer}")
    print(f"tokens: {model.tokens}  trace: {trace}")


main()
