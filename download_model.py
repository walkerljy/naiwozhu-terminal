# -*- coding: utf-8 -*-
"""
download_model.py —— 下载 Vosk 中文离线模型到 models/
用法：python download_model.py
相关文件：目标路径取自 config.VOSK_MODEL，下载后供 stt.py 使用。
（原来的 setup.py 与 setuptools 的约定重名，将来引入 pyproject 会冲突，故改名。）
"""
import sys
import zipfile
from pathlib import Path

import config

MODEL_URL = "https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip"


def _progress(done, size, total):
    if total > 0:
        pct = min(100, done * size * 100 // total)
        print(f"\r  下载中 {pct}%", end="", flush=True)


def main():
    target = Path(config.VOSK_MODEL)
    if target.exists():
        print("模型已存在，跳过：", target)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / "_model.zip"

    print("下载 Vosk 中文小模型（约 40MB）…")
    import urllib.request
    try:
        urllib.request.urlretrieve(MODEL_URL, tmp, _progress)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"下载失败：{e}\n网络不好的话可以手动下载后解压到 {target.parent}")
    print()

    with zipfile.ZipFile(tmp) as z:
        # 防 zip 路径穿越（源是官方的，但这行成本很低）
        for name in z.namelist():
            if name.startswith(("/", "\\")) or ".." in Path(name).parts:
                raise SystemExit(f"压缩包里有可疑路径：{name}")
        z.extractall(target.parent)
    tmp.unlink(missing_ok=True)

    if not target.exists():
        raise SystemExit(f"解压后没找到 {target}，检查压缩包结构")
    print("完成。下一步：python gen_voice.py --list 看看要准备哪些语音素材")


if __name__ == "__main__":
    main()
