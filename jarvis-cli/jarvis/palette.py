"""Tiny shared ANSI-color helper.

Split out of cli.py so other modules (ai_client.py) can print colored
status/trace output too, without importing cli.py and creating an import
cycle (cli.py imports ai_client.py lazily to run AI prompts).
"""


class Palette:
    """ANSI color codes gated on a specific stream's isatty(), so redirecting
    stdout doesn't strip colors from stderr (or vice versa)."""

    def __init__(self, stream):
        on = stream.isatty()
        self.RESET = "\033[0m" if on else ""
        self.BOLD = "\033[1m" if on else ""
        self.CYAN = "\033[36m" if on else ""
        self.GREEN = "\033[32m" if on else ""
        self.RED = "\033[31m" if on else ""
        self.YELLOW = "\033[33m" if on else ""
        self.DIM = "\033[2m" if on else ""
