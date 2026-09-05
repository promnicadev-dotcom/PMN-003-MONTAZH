"""Create a project-local venv once; never install into system Python."""
import os
import subprocess
import sys
import venv
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    if sys.version_info < (3, 10):
        print("Python 3.10 or newer is required. Python 3.12 is recommended.")
        return 1
    env = root / ".venv"
    executable = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not executable.is_file():
        print("First run: creating a private Python environment in this folder...", flush=True)
        venv.EnvBuilder(with_pip=True).create(env)
    check = subprocess.run([str(executable), "-c", "import PIL, imageio_ffmpeg, tkinter, cv2; imageio_ffmpeg.get_ffmpeg_exe()"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if check.returncode:
        print("Installing MONTAZH dependencies from PyPI. Internet is needed for this step.", flush=True)
        installed = subprocess.run([str(executable), "-m", "pip", "install", "--disable-pip-version-check",
                                    "--only-binary=:all:", "-r", str(root / "requirements.txt")])
        if installed.returncode:
            print("Setup failed. Check your internet connection and run START.cmd again.")
            return installed.returncode
    print("Starting PMN-003 MONTAZH. Keep VOICEVOX running while making a video.", flush=True)
    return subprocess.run([str(executable), str(root / "app.py")], cwd=root).returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("Startup error:", exc)
        sys.exit(1)
