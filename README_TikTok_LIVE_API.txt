TikTok LIVE recorder API版 2026-10-14

変更点
- LIVE開始検出をyt-dlpからTikTok /api-live/user/room APIへ変更
- aid=1988 / sourceType=54 / staleTime=600000 / Refererを使用
- status=4をOFFLINEとして判定（Streamlink現行実装と同じ）
- LIVE時はstreamDataのFLV URLを取得しFFmpegで直接録画
- H.264を優先し、既存のMP4→結合→Whisper→ダイジェスト処理を維持
- 切断時はAPIから新しい配信URLを再取得
- 保存先 F:\SCL\TikTok\YYYY\YYYYMM\YYYYMMDD_LIVE を維持
- --continuous --stall-seconds 60 はそのまま利用可能
- --probe を追加（現在状態を1回確認）

会社PCでの確認
1. 監視タスクを停止
2. C:\Scripts\record_tiktok_live.py をバックアップ後、この版に置換
3. PowerShell:
   cd C:\Scripts
   python -m py_compile record_tiktok_live.py digest_pipeline.py create_single_song.py
4. API確認:
   python record_tiktok_live.py --probe
   LIVE終了後なら OFFLINE が正常
5. タスクを開始
   既存引数: C:\Scripts\record_tiktok_live.py --continuous --stall-seconds 60

注意
次回LIVEで最終的な実録画確認は必要です。ただしAPI経路は2026-10-14に会社PCからroomIdとstreamData/FLV URL取得まで実証済みです。
