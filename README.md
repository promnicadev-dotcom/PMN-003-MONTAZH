# PMN-003 MONTAZH

**半自動映像構成装置**

<img width="1919" height="1042" alt="image" src="https://github.com/user-attachments/assets/4dcc6d3a-38d6-4811-a41a-80440e644ff5" />

<img width="1254" height="769" alt="image" src="https://github.com/user-attachments/assets/dcda9e13-77fe-4adf-a59e-4b982835831e" />


画面録画と台本から、VOICEVOXナレーション・字幕・元動画音声を合成し、ショート動画向けMP4を組み立てるWindows用デスクトップツールです。

MONTAZHはフル機能の動画編集ソフトではなく、**録画と台本を投入し、配置案を人間が必要な箇所だけ直して完成動画を出す**ことを目的にしています。

## 主な機能

- 1行＝1セリフの台本入力
- VOICEVOX / VOICEVOX Nemoによる読み上げ
- ナレーションと字幕の自動配置
- 動画全体への均等配置を初期案として生成
- タイムライン上で開始位置をドラッグ調整
- Deleteキーで不要なセリフを削除
- 元動画音声のON/OFFと音量調整
- 縦1080×1920 / 縦720×1280 / 横1920×1080出力
- アイコンの動画内表示
- 台本、字幕、設定、処理ログを出力フォルダへ保存

## Windows版を使う

GitHub Releasesから `PMN-003_MONTAZH_vX.X.X_Windows_x64.zip` を取得し、任意のフォルダへ展開してください。

1. VOICEVOXまたはVOICEVOX Nemoを起動
2. `PMN-003_MONTAZH.exe` を起動
3. 元動画を選択
4. 台本を入力
5. 「配置を確認・修正」でタイミングを調整
6. 「MP4を製造する」

完成物は既定では実行ファイルと同じ場所の `output` 以下へ保存されます。画面から別の保存先にも変更できます。

> VOICEVOX / VOICEVOX Nemo本体はMONTAZHに同梱していません。利用する音声ライブラリ側の規約・クレジット条件も確認してください。

## 台本

通常は1行に1セリフを書きます。

```text
人類は、動画編集に時間を使いすぎる。
そこで私は、動画自動製造機を製造した。
```

読み上げだけ変える場合:

```text
PMN-003を起動。 || ピーエムエヌ、ゼロゼロさんを起動。
```

時間を手動指定する場合:

```text
[00:08-00:14]
この場面で読むセリフ。
```

## ソースから起動

Python 3.12を推奨します。

```text
START.cmd
```

初回にプロジェクト専用の仮想環境を作成し、必要なPythonパッケージを導入します。

## Windows EXEをローカルビルド

Python 3.12をインストールしたWindowsで `BUILD_EXE.cmd` を実行してください。

生成先:

```text
dist/PMN-003_MONTAZH/PMN-003_MONTAZH.exe
```

公開版はPyInstallerの **onedir** 形式です。`PMN-003_MONTAZH.exe` だけを取り出さず、生成されたフォルダ一式で使用してください。

## 名称

**PMN-003 MONTAZH / 半自動映像構成装置**

Promnica project.
