"""What a phone job collects for its report: named screenshots with captions, an optional verdict, and the undo steps for anything it changed on the phone."""
import io
import threading

from PIL import Image

MAX_CAPTURES = 30
MAX_BYTES = 24_000_000  # all pictures of one report together
REPORT_WIDTH = 540  # a readable picture, small enough that a report with 30 of them stays a few MB
LEVELS = ("harmful", "suspicious", "nothing_found")


class Artifacts:
    def __init__(self):
        self.captures: list[dict] = []
        self.verdict: dict | None = None
        self.facts: list[str] = []  # lines tools add (an app's permissions, the device states tried)
        self._undo: list = []
        self._bytes = 0
        self._lock = threading.Lock()

    def add_capture(self, png: bytes, title: str, note: str = "", step: int = 0) -> int:
        """Keep a screenshot as a small JPEG with its caption. Returns its number (1-based)."""
        with self._lock:
            if len(self.captures) >= MAX_CAPTURES:
                raise ValueError(f"a report holds at most {MAX_CAPTURES} pictures")
            img = Image.open(io.BytesIO(png)).convert("RGB")
            img = img.resize((REPORT_WIDTH, max(1, round(img.height * REPORT_WIDTH / img.width))), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=70, optimize=True)
            jpeg = buf.getvalue()
            if self._bytes + len(jpeg) > MAX_BYTES:
                raise ValueError("the report's pictures are too big")
            self._bytes += len(jpeg)
            self.captures.append({"title": " ".join(title.split())[:80] or f"Screen {len(self.captures) + 1}", "note": " ".join(note.split())[:300], "jpeg": jpeg, "step": step})
            return len(self.captures)

    def set_verdict(self, level: str, reasons: list[str], checked: list[str], not_checked: list[str]) -> None:
        level = str(level).strip().lower().replace(" ", "_").replace("-", "_")
        level = {"safe": "nothing_found", "clean": "nothing_found", "nothing": "nothing_found", "harmless": "nothing_found",
                 "malicious": "harmful", "dangerous": "harmful", "phishing": "harmful", "suspect": "suspicious"}.get(level, level)
        if level not in LEVELS:
            raise ValueError(f"verdict level must be one of: {', '.join(LEVELS)}")
        clean = lambda xs: [" ".join(str(x).split())[:200] for x in (xs or []) if str(x).strip()][:8]
        self.verdict = {"level": level, "reasons": clean(reasons), "checked": clean(checked), "not_checked": clean(not_checked)}

    def on_finish(self, undo) -> None:
        """Register something to run when the job ends, whatever the outcome (restore the phone's display, uninstall a test app, ...)."""
        self._undo.append(undo)

    def finish(self) -> list[str]:
        """Run the undo steps, last first. Returns the errors (a phone that went away must not stop the rest)."""
        errors = []
        while self._undo:
            fn = self._undo.pop()
            try:
                fn()
            except Exception as e:
                errors.append(f"{type(e).__name__}: {e}"[:160])
        return errors
