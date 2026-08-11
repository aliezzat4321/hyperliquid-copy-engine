from __future__ import annotations

import os
import resource
import time


class Progress:
    def __init__(self, label: str, *, every: int = 20) -> None:
        self.label = label
        self.every = max(1, every)
        self.started = time.monotonic()
        self.count = 0

    def tick(self, detail: str = "") -> None:
        self.count += 1
        if self.count == 1 or self.count % self.every == 0:
            rss_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            elapsed = time.monotonic() - self.started
            print(
                f"progress label={self.label} count={self.count} elapsed_s={elapsed:.1f} "
                f"maxrss_mib={rss_kib / 1024:.1f} pid={os.getpid()} {detail}".rstrip(),
                flush=True,
            )
