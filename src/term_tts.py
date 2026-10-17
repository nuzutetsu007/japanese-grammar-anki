#!/usr/bin/env python3
"""术语的读音朗读(VOICEVOX + ffmpeg)。

跟 `tts.py` 同一套路子,只是换成读 term 表:
  * 读 **reading(假名)** —— 汉字词用书里给的振り仮名,保证读音对;片假名词直接读词面
  * 音色在 四国めたん(2) / 波音リツ(9) / ずんだもん(3) 里按 id 稳定随机,重跑不变
  * 内容寻址命名(音色+参数+文本 的 sha1),改文本 = 新文件,不会串
  * wav 不落盘:VOICEVOX 出来的 wav 直接管进 ffmpeg 转 mp3
  * 幂等:已有 mp3 且没 --force 就跳过;跑完把 audio/audio_speaker 写回库里
  * 只给会做成卡的词做(establish/common_claim),uncertain 不做

依赖:VOICEVOX engine 在跑(默认 127.0.0.1:50021)、系统有 ffmpeg。
    docker run -d --rm --name voicevox-tmp --gpus all -p 50021:50021 \
        voicevox/voicevox_engine:nvidia-latest

用法:
    .venv/bin/python src/term_tts.py --dry          # 只看要合成多少条
    .venv/bin/python src/term_tts.py --workers 6
    .venv/bin/python src/term_tts.py --speaker 9    # 钉死音色
    .venv/bin/python src/term_tts.py --gc           # 删掉没人引用的 mp3
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import MEDIA_DIR, connect, init_db, log_run, now   # noqa: E402

TTS_VERSION = "term_tts_v1"
SPEAKERS = [2, 9, 3]                       # 四国めたん / 波音リツ / ずんだもん
SPEAKER_NAMES = {2: "四国めたん", 3: "ずんだもん", 9: "波音リツ"}
BASE = os.environ.get("VOICEVOX_URL", "http://127.0.0.1:50021").rstrip("/")
AUDIO_PARAMS = {"rate": 24000, "stereo": False, "speed": 1.05, "volume": 1.0, "q": 5}


def speak_text(r) -> str:
    """念什么:有假名读音就念假名(读音最准),否则念词面。"""
    return (r["reading"] or r["term"]).strip()


def pick_speaker(term_id: int, forced: int = 0, reshuffle: bool = False) -> int:
    if forced:
        return forced
    rng = random.Random(f"tts:term:{term_id}:{TTS_VERSION}" + (":re" if reshuffle else ""))
    return rng.choice(SPEAKERS)


def audio_name(text: str, speaker: int) -> str:
    key = f"{speaker}|{json.dumps(AUDIO_PARAMS, sort_keys=True)}|{text}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16] + ".mp3"


def synth_mp3(text: str, speaker: int, out: Path) -> int:
    import httpx

    q = httpx.post(f"{BASE}/audio_query", params={"text": text, "speaker": speaker}, timeout=90)
    q.raise_for_status()
    aq = q.json()
    aq["outputSamplingRate"] = AUDIO_PARAMS["rate"]
    aq["outputStereo"] = AUDIO_PARAMS["stereo"]
    aq["speedScale"] = AUDIO_PARAMS["speed"]
    aq["volumeScale"] = AUDIO_PARAMS["volume"]
    w = httpx.post(f"{BASE}/synthesis", params={"speaker": speaker}, json=aq, timeout=180)
    w.raise_for_status()
    tmp = out.with_suffix(".part")
    r = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "wav", "-i", "-",
         "-f", "mp3", "-codec:a", "libmp3lame", "-qscale:a", str(AUDIO_PARAMS["q"]),
         "-ar", str(AUDIO_PARAMS["rate"]), "-ac", "1", str(tmp)],
        input=w.content, capture_output=True)
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg 失败: {r.stderr.decode('utf-8', 'replace')[:200]}")
    tmp.replace(out)                       # 原子落盘
    return out.stat().st_size


def gc(conn) -> int:
    """删掉没有任何活跃引用(example / term)的 mp3。"""
    live = set()
    for tbl in ("example", "term"):
        for (a,) in conn.execute(f"SELECT audio FROM {tbl} WHERE audio IS NOT NULL AND audio<>''"):
            live.add(a)
    n = 0
    for p in MEDIA_DIR.glob("*.mp3"):
        if p.name not in live:
            p.unlink()
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="术语读音朗读")
    ap.add_argument("--category", default="")
    ap.add_argument("--min-star", type=int, default=0, dest="min_star")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--speaker", type=int, default=0, help="固定音色 id")
    ap.add_argument("--force", action="store_true", help="已有的也重做")
    ap.add_argument("--reshuffle", action="store_true", help="重新随机音色")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--gc", action="store_true")
    a = ap.parse_args()

    conn = connect()
    init_db(conn)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)

    if a.gc:
        print(f"[term_tts] gc 删掉 {gc(conn)} 个没人引用的 mp3")

    q = ("SELECT id, term, reading, category, difficulty, audio, audio_speaker FROM term"
         " WHERE confidence IN ('established','common_claim')")
    p: list = []
    if a.category:
        q += " AND category=?"
        p.append(a.category)
    if a.min_star:
        q += " AND difficulty>=?"
        p.append(a.min_star)
    q += " ORDER BY difficulty DESC, hits DESC, term"
    if a.limit:
        q += f" LIMIT {int(a.limit)}"
    rows = [dict(r) for r in conn.execute(q, p)]

    todo, skipped = [], 0
    for r in rows:
        if (not a.force and not a.reshuffle and r["audio"]
                and (MEDIA_DIR / r["audio"]).exists()):
            skipped += 1
            continue
        todo.append(dict(r, text=speak_text(r),
                         speaker=pick_speaker(r["id"], a.speaker, a.reshuffle)))

    dist: dict[int, int] = {}
    for r in todo:
        dist[r["speaker"]] = dist.get(r["speaker"], 0) + 1
    print(f"[term_tts] 待合成 {len(todo)} 条(已有跳过 {skipped})"
          f" / 音色 " + ", ".join(f"{SPEAKER_NAMES.get(k, k)}:{v}" for k, v in sorted(dist.items())))
    if a.dry:
        for r in todo[:5]:
            print(f"      [dry] {r['term']} ← 「{r['text']}」 {SPEAKER_NAMES.get(r['speaker'])}")
        return 0

    t0, ok, fail, total_bytes = time.time(), 0, 0, 0

    def work(r):
        name = audio_name(r["text"], r["speaker"])
        path = MEDIA_DIR / name
        if not path.exists():
            synth_mp3(r["text"], r["speaker"], path)
        return r, name, path.stat().st_size

    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        futs = [ex.submit(work, r) for r in todo]
        for f in as_completed(futs):
            try:
                r, name, size = f.result()
            except Exception as e:  # noqa: BLE001
                fail += 1
                print(f"    [fail] {type(e).__name__}: {str(e)[:120]}", flush=True)
                continue
            conn.execute("UPDATE term SET audio=?, audio_speaker=?, updated_at=? WHERE id=?",
                         (name, r["speaker"], now(), r["id"]))
            ok += 1
            total_bytes += size
            if ok % 100 == 0:
                conn.commit()
                print(f"    {ok}/{len(todo)}  {time.time() - t0:.0f}s  "
                      f"{total_bytes / 1e6:.1f}MB", flush=True)
    conn.commit()
    log_run(conn, "term_tts", TTS_VERSION, now(), ok, fail,
            f"speakers={dist} files={ok} bytes={total_bytes}")
    print(f"[term_tts] 合成 {ok} / 失败 {fail} / {total_bytes / 1e6:.1f}MB / "
          f"{time.time() - t0:.0f}s → {MEDIA_DIR}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
