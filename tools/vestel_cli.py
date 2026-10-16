#!/usr/bin/env python3
"""Command line tool for Vestel/Telefunken SmartCenter TVs.

Examples:
  vestel_cli.py discover
  vestel_cli.py 192.168.1.50 info
  vestel_cli.py 192.168.1.50 key VOL_UP VOL_UP OK
  vestel_cli.py 192.168.1.50 channels
  vestel_cli.py 192.168.1.50 channel "Das Erste HD"
  vestel_cli.py 192.168.1.50 timers
  vestel_cli.py 192.168.1.50 followtv --serve 8090 --vlc
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "vestel_smartcenter"))

import contextlib

import aiohttp
from aiohttp import web
from pyvestel import (
    FollowTVManager,
    VestelError,
    VestelTV,
    discover,
    parse_stream_url,
    stream_ts,
)


def _dump(obj: object) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


async def _tv(session: aiohttp.ClientSession, args: argparse.Namespace) -> VestelTV:
    tv = await VestelTV.from_host(session, args.host, location=args.location)
    if tv.info.is_v2:
        await tv.connect()  # v2 answers arrive on ws://<tv>:7681
    return tv


def _vlc_cmd(url: str) -> list[str]:
    vlc = shutil.which("vlc") or shutil.which("cvlc") or "vlc"
    return [vlc, "--rtsp-http", "--rtsp-http-port=8554", url]


async def cmd_followtv(tv: VestelTV, args: argparse.Namespace) -> None:
    mgr = FollowTVManager(tv, http_port=None if args.no_http_tunnel else args.tunnel_port, idle_stop=3600)
    if args.serve is None and args.out is None:
        url = await mgr.start_on_tv()
        if not url:
            print(f"FollowTV failed: {mgr.last_error}", file=sys.stderr)
            return
        target = parse_stream_url(url)
        print(f"raw:  {url}\nurl:  {target.url}\nkey:  {target.key.hex() if target.key else '-'}")
        if target.key:
            print("Encrypted (AES) -> stock VLC cannot play it, use --serve.")
        else:
            print("VLC:  " + " ".join(_vlc_cmd(target.url)))
            if args.vlc:
                subprocess.Popen(_vlc_cmd(target.url))
        return

    if args.out:
        url = await mgr.start_on_tv()
        if not url:
            print(f"FollowTV failed: {mgr.last_error}", file=sys.stderr)
            return
        n = 0
        with open(args.out, "wb") as fh:
            async for chunk in stream_ts(url, None if args.no_http_tunnel else args.tunnel_port):
                fh.write(chunk)
                n += len(chunk)
                if args.mb and n > args.mb * 1_000_000:
                    break
        with contextlib.suppress(VestelError):
            await tv.stop_followtv()
        print(f"wrote {n} bytes to {args.out}")
        return

    async def handle(request: web.Request) -> web.StreamResponse:
        q = mgr.subscribe()
        resp = web.StreamResponse(headers={"Content-Type": "video/mp2t", "Cache-Control": "no-cache"})
        await resp.prepare(request)
        try:
            while (chunk := await q.get()) is not None:
                await resp.write(chunk)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            mgr.unsubscribe(q)
        return resp

    app = web.Application()
    app.router.add_get("/stream.ts", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, args.bind, args.serve)
    await site.start()
    local = f"http://127.0.0.1:{args.serve}/stream.ts"
    print(f"FollowTV proxy: http://{args.bind}:{args.serve}/stream.ts  (Ctrl+C to stop)")
    if args.vlc:
        subprocess.Popen([shutil.which("vlc") or "vlc", local])
    try:
        await asyncio.Event().wait()
    finally:
        await mgr.stop()
        await runner.cleanup()


async def main(args: argparse.Namespace) -> None:
    async with aiohttp.ClientSession() as session:
        if args.host == "discover":
            tvs = await discover(session, timeout=args.timeout)
            _dump([asdict(t) for t in tvs] or "no SmartCenter TV found")
            return
        tv = await _tv(session, args)
        try:
            c = args.cmd
            if c == "info":
                _dump({**asdict(tv.info), "is_v2": tv.info.is_v2})
            elif c == "key":
                for k in args.args:
                    await tv.send_key(k)
                    await asyncio.sleep(0.25)
            elif c == "text":
                await tv.send_text(" ".join(args.args))
            elif c == "volume":
                if args.args:
                    await tv.set_volume(int(args.args[0]))
                print(await tv.get_volume())
            elif c == "channels":
                _dump([asdict(ch) for ch in await tv.get_channels()])
            elif c == "channel":
                want = " ".join(args.args)
                chans = await tv.get_channels()
                ch = next((x for x in chans if str(x.rsn) == want or x.name.lower() == want.lower()), None)
                if ch is None:
                    sys.exit(f"channel not found: {want}")
                await tv.set_channel(ch.rsn)
            elif c == "current":
                cur = await tv.get_current_channel()
                _dump(asdict(cur) if cur else None)
            elif c == "state":
                print(await tv.get_state())
            elif c == "timers":
                _dump([asdict(t) for t in await tv.get_reminders()])
            elif c == "records":
                _dump([asdict(t) for t in await tv.get_recordings()])
            elif c in ("add-timer", "add-record"):
                # args: <rsn> <start ISO> <end ISO> <name> [o|d|w]
                rsn, start, end, name, *rep = args.args
                chans = await tv.get_channels()
                ch = next(x for x in chans if x.rsn == int(rsn))
                new_id = await tv.add_timed(
                    "timer" if c == "add-timer" else "record",
                    ch,
                    name,
                    datetime.fromisoformat(start).astimezone(),
                    datetime.fromisoformat(end).astimezone(),
                    rep[0] if rep else "o",
                )
                print(f"id: {new_id}")
            elif c in ("del-timer", "del-record"):
                ok = await tv.delete_timed("timer" if c == "del-timer" else "record", int(args.args[0]))
                print("ok" if ok else "failed")
            elif c == "url":
                print(await tv.open_url(args.args[0]))
            elif c == "eco":
                await tv.set_eco_mode(args.args[0])
            elif c == "apps":
                _dump(await tv.get_special_apps())
            elif c == "wol":
                tv.wake_on_lan()
            elif c == "events":
                tv.add_listener(lambda e: print(f"{datetime.now():%H:%M:%S} {e}", flush=True))
                await tv.connect()
                await asyncio.Event().wait()
            elif c == "followtv":
                await cmd_followtv(tv, args)
            else:
                sys.exit(f"unknown command {c}")
        finally:
            await tv.disconnect()


def cli() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("host", help="TV IP or 'discover'")
    p.add_argument(
        "cmd",
        nargs="?",
        default="info",
        help="info|key|text|volume|channels|channel|current|state|timers|records|"
        "add-timer|add-record|del-timer|del-record|url|eco|apps|wol|events|followtv",
    )
    p.add_argument("args", nargs="*")
    p.add_argument("--location", help="dd.xml URL (skip SSDP), e.g. http://IP:PORT/dd.xml")
    p.add_argument("--timeout", type=float, default=4.0)
    p.add_argument("--serve", type=int, help="followtv: run HTTP proxy on this port")
    p.add_argument("--bind", default="0.0.0.0")  # noqa: S104
    p.add_argument("--out", help="followtv: write decrypted TS to file")
    p.add_argument("--mb", type=int, default=0, help="followtv --out: stop after N MB")
    p.add_argument("--vlc", action="store_true", help="followtv: launch VLC")
    p.add_argument("--no-http-tunnel", action="store_true", help="followtv: plain RTSP/TCP")
    p.add_argument("--tunnel-port", type=int, default=8554, help="followtv: RTSP-over-HTTP port")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        pass
    except VestelError as err:
        sys.exit(f"error: {err}")


if __name__ == "__main__":
    cli()
