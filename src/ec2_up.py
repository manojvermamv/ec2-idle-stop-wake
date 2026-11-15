#!/usr/bin/env python3
"""SSH helper: wake EC2 on demand, wait for it, then carry the same SSH request through.

Put it in front of ssh and a normal `ssh my-host` just waits while a stopped instance
starts, then connects. Nothing has to be retried. It uses only the Python standard
library and your wake PIN (no AWS CLI, profile or sign-in). In ~/.ssh/config:

    ProxyCommand python /path/to/ec2_up.py WAKE_URL %h %p --pin-file ~/.ssh/ec2-wake.pin

How it connects:
  1. Fast path: %h:%p (an Elastic IP or DNS name) is tried for 3 seconds. If the
     instance is already up it answers, and the wake page is not called at all.
  2. Otherwise the wake page starts the instance (it waits on the server side, so
     this is about one request), reports its current IP, and that IP is used.
With a changing IP, set HostName to a made-up alias so step 1 simply fails fast.
"""

import argparse
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

FAST_TIMEOUT = 3      # seconds to try the address from ssh before calling the wake page
WAIT_STEP = 25        # seconds the wake page may wait per request
ENDED = {"shutting-down", "terminated"}


def call(args, action, wait=0):
    """POST to the wake page. Returns (HTTP status, JSON body)."""
    body = json.dumps({"action": action, "pin": args.pin, "wait": wait}).encode("utf-8")
    request = urllib.request.Request(args.url, data=body, method="POST",
                                     headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=wait + 15) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        try:
            data = json.loads(error.read() or b"{}")
        except ValueError:
            data = {}
        if error.code in (400, 403, 405, 429) or error.code >= 500:
            raise RuntimeError(data.get("error") or f"wake page returned HTTP {error.code}") from None
        return error.code, data


def connect(address, port, timeout):
    connection = socket.create_connection((address, port), timeout=timeout)
    connection.settimeout(None)
    return connection


def wait_for_ssh(args):
    """Return (address, connection) once SSH answers, waking the instance if needed."""
    deadline = time.monotonic() + args.timeout
    if args.host:
        try:
            return args.host, connect(args.host, args.port, FAST_TIMEOUT)
        except OSError:
            pass  # not up (or not a fixed address): ask the wake page
    action = "status" if args.no_wake else "wake"
    announced = False
    last = "waiting for the instance"
    while time.monotonic() < deadline:
        wait = 0 if args.no_wake else max(0, min(WAIT_STEP, int(deadline - time.monotonic()) - 5))
        _, data = call(args, action, wait)
        state = data.get("state", "unknown")
        if state in ENDED:
            raise RuntimeError(f"the instance is {state}")
        if args.no_wake and state != "running":
            raise RuntimeError(f"the instance is {state}, and --no-wake is set")
        if state != "running" and not announced:
            print(f"Waking EC2 (state {state}). Your SSH request will continue by itself.",
                  file=sys.stderr)
            announced = True
        last = f"state {state}"
        address = data.get("privateIp" if args.private_ip else "publicIp")
        if state == "running" and address:
            try:
                connection = connect(address, args.port, 5)
            except OSError as error:   # running, but sshd needs a few more seconds
                last = f"{address}:{args.port} {error}"
            else:
                if announced:
                    print("EC2 is ready.", file=sys.stderr)
                return address, connection
        elif state == "running":
            last = "the instance has no " + ("private" if args.private_ip else "public") + " IP"
        time.sleep(2 if state == "running" else 1)
    raise RuntimeError(f"SSH not reachable after {args.timeout} seconds. Last check: {last}.")


def relay(connection):
    if os.name == "nt":
        import msvcrt
        for stream in (sys.stdin, sys.stdout):
            msvcrt.setmode(stream.fileno(), os.O_BINARY)

    def send_input():
        try:
            while True:
                chunk = os.read(sys.stdin.fileno(), 65536)
                if not chunk:
                    break
                connection.sendall(chunk)
            connection.shutdown(socket.SHUT_WR)
        except OSError:
            return

    threading.Thread(target=send_input, daemon=True).start()
    while True:
        chunk = connection.recv(65536)
        if not chunk:
            break
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Wake EC2 on demand and carry an SSH request through.")
    parser.add_argument("url", help="wake page URL (WakePageUrl in the stack outputs)")
    parser.add_argument("host", nargs="?", help="ssh's %%h: tried first, so a running instance skips the wake page")
    parser.add_argument("port", nargs="?", type=int, default=22, help="ssh's %%p (default 22)")
    parser.add_argument("--pin-file", help="file with your wake PIN (default: the EC2_WAKE_PIN variable)")
    parser.add_argument("--timeout", type=int, default=300, help="seconds to wait in total (default 300)")
    parser.add_argument("--private-ip", action="store_true", help="connect to the private IP")
    parser.add_argument("--no-wake", action="store_true", help="never start it; only connect if already running")
    parser.add_argument("--check", action="store_true", help="wake and report the address, without relaying SSH")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535 or args.timeout < 1:
        parser.error("use a valid port and a positive timeout")
    if args.pin_file:
        args.pin = Path(args.pin_file).expanduser().read_text(encoding="utf-8").strip()
    else:
        args.pin = os.environ.get("EC2_WAKE_PIN", "").strip()
    if not args.pin:
        parser.error("pass --pin-file or set EC2_WAKE_PIN")

    address, connection = wait_for_ssh(args)
    with connection:
        if args.check:
            print(f"EC2 is running. SSH port reachable at {address}:{args.port}.")
        else:
            relay(connection)


if __name__ == "__main__":
    try:
        main()
    except (KeyError, OSError, ValueError, RuntimeError) as error:
        sys.exit(f"ec2-up: {error}")
