"""Isolated worker process for trafilatura.extract.

lxml's C extension can segfault (access violation 0xc0000005) on certain
pages (seen 2026-08-17 on Wharfedale review pages) -- that kills the whole
bot process since a native crash can't be caught with try/except. Running
extraction here means only this short-lived subprocess dies; the bot stays
up and falls back to a plain regex strip.
"""
import sys

import trafilatura


def main() -> None:
    html = sys.stdin.buffer.read().decode("utf-8", "ignore")
    text = trafilatura.extract(
        html, include_tables=True, include_comments=False, favor_recall=True
    )
    sys.stdout.buffer.write((text or "").encode("utf-8"))


if __name__ == "__main__":
    main()
