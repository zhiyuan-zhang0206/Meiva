import sys
from dataclasses import replace
from pathlib import Path

from base.api_contracts.usage import ReceiptSubmission, UsageLedger
from base.host.exit_codes import hard_exit
from base.lm.usage.receipt import UsageReceiptDelivery, UsageReceiptStore, decode_receipt


def main() -> None:
    store = UsageReceiptStore(Path(sys.argv[1]), "cluster")
    payload = sys.stdin.buffer.read()
    if len(sys.argv) == 2:
        store.recover(payload)
        hard_exit(0)
    prefix = sys.argv[2]
    original = decode_receipt(payload)
    ledger = UsageLedger(lambda _: False, lambda: "test", store.cluster, lambda: "unused")
    delivery = UsageReceiptDelivery(store, ledger)
    for index in range(100):
        entry = replace(original, call_id=f"{prefix}:{index}")
        assert delivery.submit(entry) is ReceiptSubmission.STAGED
    hard_exit(0)


if __name__ == "__main__":
    main()
