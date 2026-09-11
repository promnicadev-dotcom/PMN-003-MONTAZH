# PMN-003 MONTAZH v0.3.5 Test Report

## 変更内容
- 固定 `PROMNICA / FIELD REPORT` をプロジェクト設定 `header_text` に変更。
- 詳細設定からヘッダー文字列を変更可能。空欄なら非表示。
- タイトル解析をPMN専用から一般化。
  - `製品番号 英語名 日本語名` の3要素に見える場合のみ、1行目を `製品番号 / 英語名`、2行目を日本語名として表示。
  - 例: `AX-12 CLEANER 重複整理装置` → `AX-12 / CLEANER` + 改行 + `重複整理装置`
  - 通常の自由タイトルはそのまま表示。
- `.montazh` schema_version を2へ更新。
- schema_version 1の既存プロジェクトは旧固定ヘッダー `PROMNICA / FIELD REPORT` を補って小規模移行。

## 実施テスト
- Python構文チェック: PASS
- 一般化タイトル解析: PASS
- PMN形式の後方互換解析: PASS
- 自由タイトルが誤って3分割されないこと: PASS
- 現行schema設定復元: PASS
- schema 1 → schema 2移行: PASS
- 9:16オーバーレイ描画: PASS
  - 任意ヘッダー表示
  - ヘッダー空欄時非表示
  - 3要素タイトル時、日本語名のみ2行目へ配置

## 未確認
- Windows実機GUIでの詳細設定入力・保存・再起動後の操作感
- 実際のFFmpeg完成動画での視認性（描画ロジック自体は同一オーバーレイを使用）
