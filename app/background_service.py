"""LaunchAgent wrapper: coexist with a manually started service on port 5818."""
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import urlopen


def main():
    root = Path(__file__).resolve().parents[1]
    while True:
        try:
            with urlopen("http://127.0.0.1:5818/api/version", timeout=3) as response:
                response.read(2048)
            time.sleep(15)
        except OSError:
            subprocess.run([sys.executable, str(root / "app/server.py"), "--port", "5818"], cwd=root, start_new_session=True)
            time.sleep(10)


if __name__ == "__main__":
    main()
