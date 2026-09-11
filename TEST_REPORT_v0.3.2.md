# PMN-003 MONTAZH v0.3.2 test report

- Python syntax compile: PASS
- VOICEVOX folder resolver: direct selected folder: PASS
- VOICEVOX folder resolver: one-level nested portable folder: PASS
- Empty VOICEVOX folder setting returns no auto-start candidate: PASS
- Legacy v0.3.1 executable-path setting migration implemented: reviewed
- Main-window forced maximize removed: reviewed

Windows-only final checks still required:
- 1600x900-ish centered startup on the user's Windows display
- Selecting a VOICEVOX folder does not launch it immediately
- Restarting MONTAZH with a configured folder launches VOICEVOX/Nemo in the background when its API is unavailable
