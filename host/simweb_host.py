#!/usr/bin/env python3
"""Native messaging host for the SimWeb extension.

Commands from the extension:
  launch {url, memMB, cpuRate, cores} -> start a separate Chrome inside a
         systemd scope with a hard RAM cap, plus DevTools CPU throttling.
  stop   -> kill the simulated Chrome.
  stats  -> live memory usage of the simulated Chrome.

Internal: `simweb_host.py drive <json>` runs inside the scope, starts Chrome
with --remote-debugging-pipe and applies throttling to every tab it opens.
"""
import fcntl
import json
import math
import os
import struct
import subprocess
import sys
import time

UNIT = "simweb.scope"
PROFILE = os.path.expanduser("~/.cache/simweb-profile")
CHROME = os.environ.get("SIMWEB_CHROME", "google-chrome")
BASELINE = os.path.expanduser("~/.cache/simweb-baseline")


def read_msg():
    raw = sys.stdin.buffer.read(4)
    if len(raw) < 4:
        return None
    (n,) = struct.unpack("=I", raw)
    return json.loads(sys.stdin.buffer.read(n))


def send_msg(obj):
    data = json.dumps(obj).encode()
    sys.stdout.buffer.write(struct.pack("=I", len(data)) + data)
    sys.stdout.buffer.flush()


def systemctl(*args):
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def stop():
    systemctl("stop", UNIT)
    systemctl("reset-failed", UNIT)


def launch(cfg):
    stop()
    if os.path.exists(BASELINE):
        os.remove(BASELINE)
    # No MemoryMax yet: drive() measures the browser's own overhead first.
    subprocess.Popen(
        ["systemd-run", "--user", "--scope", "--quiet", f"--unit={UNIT}",
         "-p", "MemorySwapMax=0",
         sys.executable, os.path.abspath(__file__), "drive", json.dumps(cfg)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,  # survive Chrome killing the native host
    )
    return {"ok": True}


def stats():
    cg = systemctl("show", "-p", "ControlGroup", "--value", UNIT).stdout.strip()
    if not cg:
        return {"running": False}
    base = "/sys/fs/cgroup" + cg

    def read(name):
        with open(f"{base}/{name}") as f:
            return f.read()

    try:
        events = dict(line.split() for line in read("memory.events").splitlines())
        if not os.path.exists(BASELINE):
            return {"running": True, "measuring": True}
        with open(BASELINE) as f:
            overhead, budget = map(int, f.read().split())
        used = int(read("memory.current")) // 2**20
        return {
            "running": True,
            "overheadMB": overhead,  # idle browser + blank tab, not counted against budget
            "pageMB": max(0, used - overhead),
            "budgetMB": budget,
            "usedMB": used,
            "oomKills": int(events.get("oom_kill", 0)),
        }
    except FileNotFoundError:
        return {"running": False}


def cgroup_mb():
    with open("/proc/self/cgroup") as f:
        cg = f.read().split("::")[-1].strip()
    with open(f"/sys/fs/cgroup{cg}/memory.current") as f:
        return int(f.read()) // 2**20


def settle_mb():
    """Wait until browser memory stops changing, return it (MB)."""
    prev = -100
    for _ in range(15):
        time.sleep(1)
        cur = cgroup_mb()
        if abs(cur - prev) < 3:
            break
        prev = cur
    return cur


def drive(cfg):
    """Runs inside the scope. Owns Chrome's DevTools pipe until Chrome exits."""
    cmd_r, cmd_w = os.pipe()    # we write commands -> Chrome reads fd 3
    resp_r, resp_w = os.pipe()  # Chrome writes fd 4 -> we read

    def wire_fds():
        a = fcntl.fcntl(cmd_r, fcntl.F_DUPFD, 10)
        b = fcntl.fcntl(resp_w, fcntl.F_DUPFD, 10)
        os.dup2(a, 3)
        os.dup2(b, 4)

    os.makedirs(PROFILE, exist_ok=True)
    chrome = subprocess.Popen(
        [CHROME, "--remote-debugging-pipe", f"--user-data-dir={PROFILE}",
         "--no-first-run", "--no-default-browser-check", "about:blank"],
        preexec_fn=wire_fds, close_fds=False,
    )
    os.close(cmd_r)
    os.close(resp_w)
    out = os.fdopen(cmd_w, "wb", buffering=0)
    inp = os.fdopen(resp_r, "rb")
    next_id = [0]

    def send(method, params=None, session=None):
        next_id[0] += 1
        msg = {"id": next_id[0], "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        out.write(json.dumps(msg).encode() + b"\0")

    # Mimicked device RAM as seen by JS (navigator.deviceMemory only reports 0.25..8).
    dev_mem = min(8, max(0.25, 2 ** round(math.log2(cfg["memMB"] / 1024))))
    spoof = f"Object.defineProperty(Navigator.prototype,'deviceMemory',{{get:()=>{dev_mem}}});"

    send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True})
    buf = b""
    first_page = True
    while True:
        chunk = inp.read1(65536)
        if not chunk:
            break  # Chrome exited
        buf += chunk
        *msgs, buf = buf.split(b"\0")
        for raw in msgs:
            msg = json.loads(raw)
            if msg.get("method") != "Target.attachedToTarget":
                continue
            sid = msg["params"]["sessionId"]
            if msg["params"]["targetInfo"]["type"] == "page":
                send("Emulation.setCPUThrottlingRate", {"rate": float(cfg["cpuRate"])}, sid)
                send("Emulation.setHardwareConcurrencyOverride", {"hardwareConcurrency": int(cfg["cores"])}, sid)
                send("Page.enable", {}, sid)
                send("Page.addScriptToEvaluateOnNewDocument", {"source": spoof}, sid)
                # Auto-attach also for iframes/workers spawned by this page.
                send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}, sid)
            send("Runtime.runIfWaitingForDebugger", {}, sid)
            if first_page and msg["params"]["targetInfo"]["type"] == "page":
                # Cap = idle browser overhead + requested budget, then load the real page.
                # ponytail: one-time baseline; each extra tab's renderer overhead counts against budget.
                first_page = False
                overhead = settle_mb()
                budget = int(cfg["memMB"])
                systemctl("set-property", "--runtime", UNIT, f"MemoryMax={overhead + budget}M")
                with open(BASELINE, "w") as f:
                    f.write(f"{overhead} {budget}")
                send("Page.navigate", {"url": cfg.get("url") or "about:blank"}, sid)
    chrome.wait()


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "drive":
        return drive(json.loads(sys.argv[2]))
    msg = read_msg()
    if msg is None:
        return
    try:
        handler = {"launch": lambda: launch(msg), "stop": lambda: (stop(), {"ok": True})[1], "stats": stats}[msg["cmd"]]
        send_msg(handler())
    except Exception as e:
        send_msg({"error": str(e)})


if __name__ == "__main__":
    main()
