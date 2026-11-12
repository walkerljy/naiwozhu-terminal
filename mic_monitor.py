# -*- coding: utf-8 -*-
"""
mic_monitor.py —— 把树莓派麦克风的声音延后 N 秒放到这台电脑的扬声器上（调试用）

为什么需要它：调麦克风时你人在电脑前，喇叭在树莓派那边；听完再跑回去改参数太慢。
这个脚本把远端麦克风的声音实时搬回电脑，"延迟 1 秒"是靠先攒够 1 秒再播实现的。

    python mic_monitor.py                        # 默认延迟 1 秒
    python mic_monitor.py --delay 2              # 延迟 2 秒
    python mic_monitor.py --host nwz@192.168.137.101 --device plughw:3,0

注意：
  · 树莓派那边用 arecord 采集，**设备是独占的**——这个脚本跑着的时候，
    树莓派上的 assistant.py 打不开麦克风。听完 Ctrl+C 停掉再跑程序。
  · 延迟下限大约 0.3 秒（网络+缓冲）；给 0 也只是不预缓冲，不是真实时。
"""
import argparse
import os
import subprocess
import sys

import numpy as np
import sounddevice as sd


def read_exact(f, n):
    """从管道里读满 n 字节（管道 read 可能短读）。"""
    buf = bytearray()
    while len(buf) < n:
        b = f.read(n - len(buf))
        if not b:
            return bytes(buf)
        buf += b
    return bytes(buf)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="nwz@192.168.137.101", help="树莓派的 ssh 地址")
    ap.add_argument("--key", default=os.path.expanduser(r"~\.ssh\id_ed25519_pi"))
    ap.add_argument("--device", default="plughw:3,0", help="树莓派上的录音设备（arecord -l 查）")
    ap.add_argument("--rate", type=int, default=16000)
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--out-device", default=None, help="本机播放设备（默认用系统默认）")
    args = ap.parse_args()

    remote = (f"arecord -q -D {args.device} -f S16_LE -r {args.rate} -c 1 -t raw")
    ssh = subprocess.Popen(
        ["ssh", "-i", args.key, "-o", "LogLevel=ERROR", "-o", "BatchMode=yes",
         args.host, remote],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)

    bps = 2                                            # int16
    target = int(args.rate * args.delay) * bps
    pre = read_exact(ssh.stdout, target) if target else b""
    if len(pre) < target:
        err = ssh.stderr.read().decode("utf-8", "replace")[:300]
        print(f"[monitor] 树莓派采集没起来：{err}")
        return 1

    stream = sd.RawOutputStream(samplerate=args.rate, channels=1, dtype="int16",
                                device=args.out_device)
    stream.start()
    if pre:
        stream.write(pre)
    print(f"[monitor] 开始播放（延迟约 {args.delay:.1f} 秒）", flush=True)

    step = int(args.rate * 0.1) * bps                 # 100ms
    played = 0
    try:
        while True:
            chunk = read_exact(ssh.stdout, step)
            if len(chunk) < step:
                print("[monitor] 采集结束", flush=True)
                break
            stream.write(chunk)
            played += 1
            if played % 10 == 0:                      # 每秒报一次电平
                a = np.frombuffer(chunk, dtype=np.int16)
                print(f"[monitor] 峰值 {np.abs(a).max() / 32768:.3f}", flush=True)
    except KeyboardInterrupt:
        print("\n[monitor] 停止", flush=True)
    finally:
        stream.stop()
        stream.close()
        ssh.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
