import argparse
import json
import logging
from pathlib import Path
import subprocess
import sys
import time
import os
import signal
import threading
import re
from digest_pipeline import FileLock, save_json


def wait_for_live(base, url, interval, minutes):
    deadline = float("inf") if minutes is None else time.monotonic() + minutes * 60
    while time.monotonic() < deadline:
        cycle = time.monotonic()
        try:
            result = subprocess.run(base + ["--dump-single-json", "--skip-download", url],
                                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=max(1, min(75, deadline - time.monotonic())))
            if result.returncode == 0:
                info = json.loads(result.stdout)
                if info.get("is_live") or info.get("live_status") == "is_live":
                    logging.info("ライブ開始を検出。録画します")
                    return True
                logging.info("ライブ未開始")
            elif "not currently live" in result.stderr.lower():
                logging.info("ライブ未開始")
            else:
                logging.warning("配信状態を取得できません: %s", result.stderr.strip()[-1500:])
        except (subprocess.TimeoutExpired, ValueError) as exc:
            logging.warning("配信確認失敗: %s", exc)
        time.sleep(max(0, min(interval - (time.monotonic() - cycle), deadline - time.monotonic())))
    else:
        logging.info("待機時間内にライブ開始を確認できなかったため終了")
        return False


def download_live(base, url, args, out, prefix):
    output = str(out / f"{prefix}_%(id)s.%(ext)s")
    command = base + ["--ffmpeg-location", args.ffmpeg, "--retries", "10", "--fragment-retries", "10",
                      "--remux-video", "mp4", "--output", output, url]
    logging.info("配信終了まで録画します。保存先: %s", out)
    # Read output separately so a silent FFmpeg cannot block the watchdog.
    with (out / f"{prefix}_download.log").open("a", encoding="utf-8") as log:
        kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8", errors="replace", **kwargs)
        def read_output():
            for line in process.stdout:
                log.write(line)
                log.flush()
                try:
                    print(line, end="", flush=True)
                except OSError:
                    pass
        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        last_change = time.monotonic()
        previous = {}
        code = None
        try:
            while process.poll() is None:
                current = {}
                for path in out.glob(f"{prefix}_*"):
                    try:
                        if path.is_file():
                            st = path.stat()
                            current[path.name] = (st.st_size, st.st_mtime_ns)
                    except OSError:
                        pass
                if current != previous:
                    previous = current
                    last_change = time.monotonic()
                if time.monotonic() - last_change >= args.stall_seconds:
                    logging.warning("録画データが%d秒間更新されません。録画プロセスを停止して再監視します。取得済みデータは保持します", args.stall_seconds)
                    stop_recording(process)
                    code = 124
                    break
                time.sleep(1)
            if code is None:
                code = process.wait()
        except KeyboardInterrupt:
            logging.warning("手動停止。取得済みデータは保持します")
            stop_recording(process)
            code = 130
        reader.join(timeout=10)
        if reader.is_alive():
            logging.error("録画ログの読み取り終了を確認できません")
    logging.info("録画プロセス終了: 終了コード=%d", code)
    return code


def stop_recording(process):
    # Stop only this recorder's process tree, never unrelated FFmpeg/Python jobs.
    if os.name == "nt":
        result = subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                capture_output=True, timeout=20)
        if result.returncode and process.poll() is None:
            raise RuntimeError("録画プロセスツリーを停止できませんでした")
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=10)


def session_folder(root, stamp):
    """同じ日のLIVEは必ず YYYYMMDD_LIVE へ集約する。"""
    year = stamp[:4]
    month = stamp[:6]
    day = stamp[:8]
    parent = root / year / month
    parent.mkdir(parents=True, exist_ok=True)
    return parent / f"{day}_LIVE"



def validate_media(ffmpeg, path):
    """FFmpegで最後まで読み切れることを確認する。元ファイルは変更しない。"""
    null_output = "NUL" if os.name == "nt" else "/dev/null"
    command = [str(ffmpeg), "-hide_banner", "-loglevel", "error",
               "-i", str(path), "-map", "0", "-c", "copy", "-f", "null", null_output]
    result = subprocess.run(command, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        logging.warning("動画検証失敗: %s / %s", path.name, result.stderr.strip()[-1500:])
        return False
    return path.is_file() and path.stat().st_size > 0


def recover_part_files(args, out, account):
    """異常終了で残った .mp4.part を安全にMP4へ復旧する。

    復旧中は .recovering.mp4 に出力し、検証成功後だけ正式な .mp4 に置換する。
    元の .mp4.part は正式MP4の検証成功後にだけ削除する。失敗時は必ず残す。
    """
    recovered = 0
    failed = 0
    for part in sorted(out.glob(f"{account}_*_part*_*.mp4.part")):
        target = part.with_suffix("")
        temp = target.with_name(target.stem + ".recovering.mp4")

        if target.exists():
            if validate_media(args.ffmpeg, target):
                logging.info("復旧済みMP4を確認: %s", target.name)
                try:
                    part.unlink()
                    logging.info("元の.partを削除: %s", part.name)
                except OSError as exc:
                    logging.warning("復旧済みですが元の.partを削除できません: %s", exc)
                recovered += 1
                continue
            logging.warning("同名MP4が存在しますが検証に失敗したため、.partは保持します: %s", target.name)
            failed += 1
            continue

        try:
            if temp.exists():
                temp.unlink()
            logging.info(".part自動復旧開始: %s", part.name)
            command = [str(args.ffmpeg), "-hide_banner", "-loglevel", "warning",
                       "-err_detect", "ignore_err", "-i", str(part),
                       "-map", "0", "-c", "copy", "-movflags", "+faststart",
                       "-y", str(temp)]
            result = subprocess.run(command, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace")
            if result.returncode != 0 or not temp.exists() or temp.stat().st_size == 0:
                logging.error(".part復旧失敗。元ファイルは保持します: %s / %s",
                              part.name, result.stderr.strip()[-1500:])
                temp.unlink(missing_ok=True)
                failed += 1
                continue
            if not validate_media(args.ffmpeg, temp):
                logging.error("復旧MP4の検証失敗。元ファイルは保持します: %s", part.name)
                temp.unlink(missing_ok=True)
                failed += 1
                continue

            temp.replace(target)
            if not validate_media(args.ffmpeg, target):
                logging.error("正式MP4への確定後検証に失敗。.partは保持します: %s", part.name)
                failed += 1
                continue
            part.unlink()
            logging.info(".part自動復旧成功: %s -> %s", part.name, target.name)
            recovered += 1
        except (OSError, subprocess.SubprocessError) as exc:
            logging.exception(".part復旧中にエラー。元ファイルは保持します: %s / %s", part.name, exc)
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            failed += 1

    if recovered or failed:
        logging.info(".part復旧結果: 成功%d / 失敗%d", recovered, failed)
    return recovered, failed


def recording_files(out, account):
    files=[]
    for path in sorted(out.glob(f"{account}_*_part*_*")):
        if path.is_file() and path.name.endswith(".mp4") and not path.name.endswith(".recovering.mp4"):
            files.append(path)
    return files

def next_part_number(out, account):
    highest=0
    pattern=re.compile(rf"^{re.escape(account)}_\d{{8}}_\d{{6}}_part(\d{{4}})_")
    for path in out.glob(f"{account}_*_part*"):
        m=pattern.match(path.name)
        if m: highest=max(highest,int(m.group(1)))
    return highest+1

def save_recording_state(out, account, stamp, status):
    save_json(out / "recording_state.json", {"status":status,"account":account,"date":stamp[:8],"last_start":stamp,"updated":time.strftime("%Y-%m-%d %H:%M:%S"),"inputs":[str(p.resolve()) for p in recording_files(out,account)]})

def start_postprocess(args, out, inputs, stamp):
    if args.no_digest or not inputs:
        return
    config = json.loads(Path(args.digest_config).read_text(encoding="utf-8-sig"))
    python = Path(config["whisper_python"])
    if not python.is_file():
        logging.error("文字起こし用Pythonが見つかりません。録画ファイルは保持します: %s", python)
        return
    log_path = out / f"{stamp}_postprocess.log"
    command = [str(python), "-X", "utf8", str(Path(__file__).with_name("digest_pipeline.py")),
               "--input", *inputs, "--output", str(out.resolve()),
               "--config", str(Path(args.digest_config).resolve()), "--date", stamp[:8]]
    with log_path.open("a", encoding="utf-8") as log:
        subprocess.Popen(command, stdout=log, stderr=log,
                         creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


def finish_session(args, account, stamp, out, prefixes=None):
    # Windows再起動などで残った .mp4.part を、後処理へ渡す前に復旧する。
    recover_part_files(args, out, account)
    inputs=[str(path.resolve()) for path in recording_files(out,account)]
    save_recording_state(out,account,stamp,"finished")
    if args.no_digest or not inputs:
        logging.info("後処理対象なし、または後処理無効。録画ファイルは保持します")
        return
    save_json(out / "session.json", {"status":"pending","inputs":inputs,"config":str(Path(args.digest_config).resolve()),"date":f"{stamp[:4]}/{stamp[4:6]}/{stamp[6:8]}"})
    logging.info("配信を後処理に登録しました: %s / 素材%d本",out,len(inputs))
    start_postprocess(args,out,inputs,stamp)

def capture_session(base, url, args, account, stamp, out, bounded):
    last_code=0; attempt=0; part_no=next_part_number(out,account)
    save_recording_state(out,account,stamp,"recording")
    try:
        while True:
            prefix=f"{account}_{stamp}_part{part_no:04d}"
            logging.info("録画パート開始: part%04d",part_no)
            code=download_live(base,url,args,out,prefix)
            save_recording_state(out,account,stamp,"reconnect_wait")
            if code==130: return 130
            last_code=code
            if bounded and attempt>=args.max_restarts:
                logging.warning("再録画上限に達しました。配信途中の可能性があるため後処理は保留します")
                return code or 1
            attempt+=1; part_no+=1
            logging.info("最大%d分の再開待ち。再開した動画は同じ配信として扱います",args.reconnect_minutes)
            remaining=args.reconnect_minutes*60-args.interval
            time.sleep(min(args.interval,args.reconnect_minutes*60))
            if remaining<=0 or not wait_for_live(base,url,args.interval,remaining/60):
                logging.info("再開待ち時間が経過。配信終了として後処理を開始します")
                finish_session(args,account,stamp,out)
                return last_code
            save_recording_state(out,account,stamp,"recording")
    except KeyboardInterrupt:
        logging.info("手動停止。録画ファイルは保持。自動後処理は開始しません")
        save_recording_state(out,account,stamp,"manual_stop")
        return 130

def record_session(base, url, args, account, stamp, out):
    logging.info("開始待ち: %s / 確認間隔%d秒 / 最大待機%d分", url, args.interval, args.wait_minutes)
    if not wait_for_live(base, url, args.interval, args.wait_minutes):
        return 0
    live_out = session_folder(out, stamp)
    live_out.mkdir(parents=True, exist_ok=True)
    return capture_session(base, url, args, account, stamp, live_out, True)


def continuous_session(base, url, args, account, out):
    logging.info("常時監視開始。終了した配信の後処理は別プロセスで実行します")
    try:
        while True:
            if wait_for_live(base, url, args.interval, None):
                stamp = time.strftime('%Y%m%d_%H%M%S')
                live_out = session_folder(out, stamp)
                live_out.mkdir(parents=True, exist_ok=True)
                # PC再起動前の異常終了素材があれば、新しい録画開始前にも復旧を試みる。
                recover_part_files(args, live_out, account)
                existing = recording_files(live_out, account)
                if existing:
                    logging.info("同日既存録画を%d本検出。PC再起動前の素材も同じLIVEフォルダで引き継ぎます", len(existing))
                if capture_session(base, url, args, account, stamp, live_out, False) == 130:
                    return 130
    except KeyboardInterrupt:
        logging.info("常時監視を停止しました")
    return 130


def main():
    parser = argparse.ArgumentParser(description="TikTok LIVEの開始を待って1配信を録画")
    parser.add_argument("--account", default="moa_pi")
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--wait-minutes", type=int, default=180)
    parser.add_argument("--outdir", default=r"F:\SCL\TikTok")
    parser.add_argument("--ffmpeg", default=r"F:\SCL\TOOLS\ffmpeg\bin\ffmpeg.exe")
    parser.add_argument("--reconnect-minutes", type=int, default=60)
    parser.add_argument("--max-restarts", type=int, default=10)
    parser.add_argument("--stall-seconds", type=int, default=180, help="録画データの更新が止まった場合の再監視までの秒数（既定180）")
    parser.add_argument("--continuous", action="store_true", help="起動中は時間制限なしで次のライブを監視")
    parser.add_argument("--digest-config", default=str(Path(__file__).with_name("digest_config.json")))
    parser.add_argument("--no-digest", action="store_true", help="後処理を無効にして録画のみ実施")
    args = parser.parse_args()
    if args.interval < 10 or args.wait_minutes < 1:
        parser.error("intervalは10秒以上、wait-minutesは1分以上で指定してください")
    if args.reconnect_minutes < 1 or args.max_restarts < 0:
        parser.error("reconnect-minutesは1以上、max-restartsは0以上で指定してください")
    if args.stall_seconds < 60:
        parser.error("stall-secondsは60秒以上で指定してください")
    account = args.account.lstrip("@")
    if not account or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_." for c in account):
        parser.error("accountにはアカウント名を指定してください")
    if not Path(args.ffmpeg).is_file():
        parser.error("FFmpegが見つかりません: " + args.ffmpeg)
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    monitor_log = Path(__file__).with_name(f"{account}_monitor.log")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(monitor_log, encoding="utf-8"), logging.StreamHandler()])
    url = f"https://www.tiktok.com/@{account}/live"
    base = [sys.executable, "-m", "yt_dlp", "--ignore-config", "--socket-timeout", "20"]
    if not args.no_digest:
        if not Path(args.digest_config).is_file():
            parser.error("digest_config.jsonが見つかりません")
        config = json.loads(Path(args.digest_config).read_text(encoding="utf-8-sig"))
        if str(Path(config["ffmpeg"]).resolve()) != str(Path(args.ffmpeg).resolve()):
            parser.error("録画と後処理のFFmpegパスを一致させてください")
    try:
        with FileLock(Path(__file__).with_name(account + ".recorder.lock")):
            if args.continuous:
                return continuous_session(base, url, args, account, out)
            return record_session(base, url, args, account, stamp, out)
    except RuntimeError as exc:
        logging.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
