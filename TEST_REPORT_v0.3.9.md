# PMN-003 MONTAZH v0.3.9 maintenance release

## Scope
No feature expansion. This release contains the v0.3.8 UX-final changes plus post-release audit fixes.

## Fixes
- Custom FFmpeg path is stored only in machine-local settings and reused when loading projects.
- `.montazh` project serialization clears `ffmpeg_path` before writing.
- FFmpeg-not-found guidance matches the Windows release workflow.
- OpenCV/Pillow license metadata is collected into the Windows release ZIP.
- Third-party project URL and FFmpeg/OpenCV notices were corrected.
- Existing GitHub Releases are never overwritten by the normal release workflow.

## Automated checks
- Python syntax check for all application modules.
- Project round-trip test with a machine-local custom FFmpeg path.
- Verification that project.json does not persist the FFmpeg executable path.
- Windows PyInstaller onedir build and ZIP generation.
- ZIP structure and third-party license-file presence check.
- Public tracked-file scan for known private-path / stale-identity markers.
