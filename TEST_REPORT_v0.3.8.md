# PMN-003 MONTAZH v0.3.8 正式版 修正報告

## 変更したファイル
- engine.py
- README.md
- README.txt
- TEST_REPORT_v0.3.8.md

## 修正内容
- `.montazh` のAtomic Save最終処理 `_fsync_file()` で、一時ファイルを `rb` ではなく `r+b` で開くよう修正。
- Windows環境でプロジェクト保存に失敗する不具合への正式修正。
- 機能追加やschema変更は行っていない。PROJECT_SCHEMA_VERSIONは3のまま。

## 確認
- engine.py / app.py / timeline_widget.py / video_preview.py / audio_preview.py のPython構文検査 PASS。
- v0.3.7からのengine.py差分が VERSION 0.3.8 と `_fsync_file()` のファイルオープンモード変更だけであることを確認。
- ユーザー環境の修正版でプロジェクト保存成功を確認済みとの報告を正式版採用根拠とする。

## 状態
- v0.3.8を正式版とする。
- 以後は原則として保守・不具合修正のみ。

## 公開前修正: SRT二重表示対策
- 焼き込み字幕と外部SRTが再生ソフトで二重表示されるのを避けるため、`subtitles.srt` を完成MP4直下から `metadata/subtitles.srt` へ移動。
- MP4本体の字幕焼き込み仕様は変更なし。
