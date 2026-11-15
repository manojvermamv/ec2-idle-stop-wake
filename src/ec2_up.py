#!/usr/bin/env python3
"""Optional SSH helper: wake EC2 through the wake page, wait for SSH, then relay.

Uses only the Python standard library and your wake PIN. No AWS CLI, profile or
sign-in is needed. Example OpenSSH setting:

    ProxyCommand python /path/to/ec2_up.py WAKE_URL --pin-file ~/.ssh/ec2-wake.pin --proxy --port %p
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


def call(args, action):
    """POST to the wake page. Returns (HTTP status, JSON body)."""
    body = json.dumps({"action": action, "pin": args.pin}).encode("utf-8")
    request = urllib.request.Request(args.url, data=body, method="POST",
                                     headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        try:
            data = json.loads(error.read() or b"{}")
        except ValueError:
            data = {}
        if error.code in (400, 403, 405, 429) or error.code >= 500:
            raise RuntimeError(data.get("error") or f"wake page returned HTTP {error.code}") from None
        return error.code, data


def wait_for_ssh(args):
    deadline = time.monotonic() + args.timeout
    action = "status" if args.no_wake else "wake"
    last = "waiting for the instance"
    announced = False
    while time.monotonic() < deadline:
        _, data = call(args, action)
        state = data.get("state", "unknown")
        if state in {"shutting-down", "terminated"}:
            raise RuntimeError(f"the instance is {state}")
        if args.no_wake and state != "running":
            raise RuntimeError(f"the instance is {state}, and --no-wake is set")
        if state != "running" and not announced:
            print(f"Waking EC2 (state {state}).", file=sys.stderr)
            announced = True
        address = data.get("privateIp" if args.private_ip else "publicIp")
        last = f"state {state}"
        if state == "running" and address:
            try:
                connection = socket.create_connection((address, args.port), timeout=5)
            except OSError as error:
                last = f"{address}:{args.port} {error}"
            else:
                connection.settimeout(None)
                return address, connection
        elif state == "running":
            last = "the instance has no " + ("private" if args.private_ip else "public") + " IP"
        time.sleep(3)
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


def main():
    parser = argparse.ArgumentParser(description="Wake EC2 through its wake page and wait for SSH.")
    parser.add_argument("url", help="wake page URL from the CloudFormation stack outputs")
    parser.add_argument("--pin-file", "--key-file", dest="pin_file",
                        help="file that holds your wake PIN (default: the EC2_WAKE_PIN variable)")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--private-ip", action="store_true", help="connect to the private IP")
    parser.add_argument("--no-wake", action="store_true", help="only connect if already running")
    parser.add_argument("--proxy", action="store_true", help="relay stdin and stdout for SSH")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or args.timeout < 1:
        parser.error("use a valid port and a positive timeout")
    if args.pin_file:
        args.pin = Path(args.pin_file).expanduser().read_text(encoding="utf-8").strip()
    else:
        args.pin = (os.environ.get("EC2_WAKE_PIN") or os.environ.get("EC2_WAKE_KEY") or "").strip()
    if not args.pin:
        parser.error("pass --pin-file or set EC2_WAKE_PIN")

    address, connection = wait_for_ssh(args)
    with connection:
        if args.proxy:
            relay(connection)
        else:
            print(f"EC2 is running. SSH port reachable at {address}:{args.port}.")


if __name__ == "__main__":
    try:
        main()
    except (KeyError, OSError, ValueError, RuntimeError) as error:
        sys.exit(f"ec2-up: {error}")
