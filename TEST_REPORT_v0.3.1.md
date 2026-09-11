# PMN-003 MONTAZH v0.3.1 test report

- Python syntax compile: PASS (`app.py`, `engine.py`)
- Version: 0.3.1
- Added machine-local VOICEVOX/Nemo executable path setting in 詳細設定
- User-selected executable path is stored in `user_settings.json`, not in `.montazh`
- Configured executable is preferred before automatic discovery
- Missing configured path falls back to existing automatic discovery
- Windows executable launch itself requires final confirmation on a Windows PC
