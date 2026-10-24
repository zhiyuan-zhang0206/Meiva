import sys
import time
from pathlib import Path

if __name__ == "__main__":
    with Path(sys.argv[1]).open("ab"):
        sys.stdout.write("open\n")
        sys.stdout.flush()
        time.sleep(60)
