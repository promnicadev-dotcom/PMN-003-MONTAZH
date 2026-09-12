PMN-003 MONTAZH v0.3.8 - Windows x64

[起動方法]
1. ZIPを任意のフォルダへ展開してください。
2. PMN-003_MONTAZH.exe を起動してください。
3. EXEだけを別の場所へ移動しないでください。_internal フォルダが必要です。

[必要なもの]
- Windows 11 / 64bit を主対象としています。
- 音声生成には VOICEVOX または VOICEVOX Nemo が別途必要です。
- 初回はMONTAZHの詳細設定からVOICEVOX/Nemoフォルダを指定できます。

[保存先]
- プロジェクト(.montazh)の保存先はユーザーが指定します。
- 既定の作業出力は、MONTAZHを展開したフォルダ内の output です。
- 共通設定はWindowsのローカル設定領域へ保存されます。

[Windowsの警告について]
この配布物はコード署名されていません。Windows SmartScreen等が警告する場合があります。
配布元とZIPのハッシュを確認した上で利用してください。

[不具合報告]
再現手順、MONTAZHのバージョン、output/logs/app.log の該当部分を添えてください。

[FFmpegについて]
この配布ZIPにはFFmpegを同梱していません。
FFmpegを別途インストールしてPATHを通すか、MONTAZHの「詳細設定」から ffmpeg.exe を指定してください。
公式: https://ffmpeg.org/download.html

初回起動時の表示は汎用テンプレートです。ヘッダー・タイトル・テーマはMONTAZH上で変更できます。

- 完成MP4と同じフォルダにSRTを置くと再生ソフトが自動読込して字幕が二重表示される場合があるため、SRTは `metadata/subtitles.srt` に出力します。

- MP4正常完成時、完成時点の .montazh を同じ成果物フォルダへ自動保存します。
