# PMN-003 MONTAZH v0.3.0 test report

Implemented and checked in this build:

- Python syntax compilation for all modules: PASS
- `.montazh` single-file project save/load: PASS
- Embedded VOICEVOX WAV cache restore from `.montazh`: PASS
- VOICEVOX cache hit on unchanged line / miss on changed key: PASS
- Duplicate-caption timeline restore by `script_index`: PASS
- Legacy/internal JSON project loading remains supported: PASS (code path retained)
- Portrait subtitle layout remains directly below source-video area: PASS (layout code retained from v0.2.11)

Windows-only behavior that still requires a real Windows machine for final confirmation:

- Automatic discovery/start of installed VOICEVOX / VOICEVOX Nemo
- Minimized/background launch behavior of the installed VOICEVOX app
- Tkinter/OpenCV review-window interaction on the user's Windows display/audio stack

