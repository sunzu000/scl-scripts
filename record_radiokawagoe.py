import asyncio
import time
import argparse
import logging
import subprocess
from pathlib import Path

import requests
import websockets

API = "https://www.jcbasimul.com/api/select_stream"
STATION = "radiokawagoe"


def get_stream_info():
    r = requests.get(
        API,
        params={
            "station": STATION,
            "channel": 0,
            "quality": "high",
            "burst": 5
        },
        timeout=10
    )
    r.raise_for_status()

    info = r.json()

    if info.get("code") != 200:
        raise RuntimeError(f"API error: {info}")

    return info


async def record(duration_sec, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    log_dir = out_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_file = out_dir / f"radiokawagoe_{stamp}.ogg"
    log_file = log_dir / f"radiokawagoe_{stamp}.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler()
        ]
    )

    end_time = time.time() + duration_sec
    total = 0

    logging.info("録音開始")
    logging.info("保存先: %s", out_file)

    with out_file.open("wb") as f:

        while time.time() < end_time:

            try:
                info = get_stream_info()

                logging.info("WebSocket: %s", info["location"])

                async with websockets.connect(
                    info["location"],
                    subprotocols=["listener.fmplapla.com"],
                    origin="https://www.jcbasimul.com",
                    max_size=None,
                    ping_interval=20,
                    ping_timeout=20
                ) as ws:

                    await ws.send(info["token"])
                    logging.info("WebSocket接続成功")

                    while time.time() < end_time:

                        try:
                            msg = await asyncio.wait_for(
                                ws.recv(),
                                timeout=15
                            )

                        except asyncio.TimeoutError:
                            logging.warning("15秒間データなし。再接続します")
                            break

                        if isinstance(msg, bytes):
                            f.write(msg)
                            f.flush()
                            total += len(msg)

            except Exception as e:
                logging.error("接続エラー: %s", e)
                await asyncio.sleep(5)

    logging.info("録音終了")
    logging.info("総サイズ: %d bytes", total)

    mp3_file = out_file.with_suffix(".mp3")

    logging.info("MP3変換開始")

    result = subprocess.run([
        r"F:\SCL\TOOLS\ffmpeg\bin\ffmpeg.exe",
        "-y",
        "-i", str(out_file),
        "-codec:a", "libmp3lame",
        "-b:a", "64k",
        str(mp3_file)
    ])

    if result.returncode == 0:
        logging.info("MP3変換成功: %s", mp3_file)
    else:
        logging.error("MP3変換失敗: returncode=%d", result.returncode)


if __name__ == "__main__":

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--duration",
        type=int,
        default=1800,
        help="録音時間（秒）"
    )

    parser.add_argument(
        "--outdir",
        default=r"F:\Backup\Recordings",
        help="保存先フォルダー"
    )

    args = parser.parse_args()

    asyncio.run(
        record(
            args.duration,
            args.outdir
        )
    )
