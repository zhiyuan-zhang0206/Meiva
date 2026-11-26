import sys
from pathlib import Path

from base.host.exit_codes import hard_exit
from base.lm.usage.receipt import UsageReceiptStore

if __name__ == "__main__":
    UsageReceiptStore(Path(sys.argv[1]), "cluster").recover(sys.stdin.buffer.read())
    hard_exit(0)
