# PMN-003 MONTAZH v0.3.5 最終安定化 テスト報告

## 変更したファイル
- engine.py
- app.py
- README.md
- README.txt
- TEST_REPORT_v0.3.5.md

## 実装した項目
1. 配置確認の段階ログ追加。既存の「画面先出し→Worker処理→UI反映」を維持し、推測による全面非同期化は実施していない。
2. 共通設定をOSのローカル設定領域へ移動。schema_version=1。既知のv0.3系旧形式だけ小規模移行。未知schemaは通知して旧ファイルを上書きしない。
3. .montazhを完全スナップショットとして保存・復元。台本、配置、話者、字幕内容、元動画参照、出力設定を含む。
4. 固定line_idは導入せず。現構造を確認した結果、review stateが字幕・読み上げ・開始時刻を同一dictに保持しているため、script_indexだけを信用しない照合へ修正。
5. .montazh Atomic Save。tempへ完全保存→ZIP/JSON検査→os.replace。
6. MP4を video.mp4.part に出力し、FFmpeg正常終了＋完成ファイル検査後のみ最終MP4へ置換。失敗時part削除。
7. VOICEVOX/Nemoの接続timeout、読込timeout、HTTPエラー、WAV不正を明示的に失敗扱い。
8. FFmpeg本書出しは進捗停止90秒を検出し terminate→kill。診断系の短処理のみ従来の総時間上限を維持。
9. 元動画はプロジェクト読込時に存在・読取・FFmpeg可読性を確認し、書出し直前にも再検査。同一パスの内容差分は許容。
10. .montazh schema_version=1。現行schemaの欠損フィールドは拒否、未知schemaは拒否。実在するv0.3.x旧形式のみ読込対応。
11. busy状態を IDLE/SAVING/LOADING/GENERATING/EXPORTING として最小管理。処理中の終了要求は安全停止後に終了。
12. statusとapp.log/render.logで処理中・成功・失敗を判別できる状態を維持・強化。

## 実施したテスト
- Python構文検査: engine.py / app.py / timeline_widget.py / video_preview.py / audio_preview.py PASS
- Project A → B → A: 手動配置 [1,10,20] / [2,12,22] / [1,10,20] が混入せず復元 PASS
- Atomic Save失敗注入: 既存A.montazhのバイト列が保持されることを確認 PASS
- 未知project schema_version=999: 明示的拒否 PASS
- 現行schemaの必須フィールド欠損: 明示的拒否 PASS
- 実在旧形式 {version, settings}: 小規模変換で読込 PASS
- 元動画消失: project load時に拒否 PASS
- 元動画が存在するが動画として不正: project load時に拒否 PASS
- 同一パスのmtime変化: 同一論理素材として扱うことを確認 PASS
- 30秒・9:16プロジェクト2件をFake VOICEVOX＋実FFmpegでフル書出し PASS
- 上記2件でtimeline.jsonの手動配置が出力へ反映 PASS
- FFmpeg進捗停止を0.5秒閾値の疑似プロセスで検出し停止 PASS
- FFmpeg失敗注入時に *.part が残らないことを確認 PASS

## 未解決事項
- ユーザー環境Windows上の実VOICEVOX/Nemoで、接続timeout・読込timeoutを意図的に発生させる実機テストは未実施。
- 過去に報告された「配置を確認」押下後フリーズはこの環境では再現できていない。今回、処理段階ログを追加し、停止箇所を実機で特定できるようにした。
- GUIの再起動を伴う共通設定永続化はコード経路を実装したが、このLinuxヘッドレス環境ではTk実機UIの再起動テストは未実施。Windows実機確認が必要。

## 仕様から逸脱した点
- なし。
- ただし「FFmpeg進捗停止検知」は本書出しに限定。probe/検査等の短時間処理には固定timeoutを残している。これは長時間処理の単純timeout禁止という要件に対する最小実装。

## 完成判定前に残す実機確認
- Windowsで異なる2つ以上の30〜90秒9:16プロジェクトを、保存→終了→再起動→読込→MP4出力まで通す。
- 実VOICEVOX停止/未起動/応答異常を確認。
- 「配置を確認」フリーズが再発した場合、output/logs/app.logの「配置確認: ...」最終行を採取する。
