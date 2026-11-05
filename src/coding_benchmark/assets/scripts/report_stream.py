"""One append-only report, streamed identically to stdout or a file."""

import datetime as dt
import json
from pathlib import Path
import sys
import threading


class ReportStream:
    def __init__(self, destination="-", format="text"):
        if format not in {"text", "ndjson"}:
            raise ValueError("Streaming format must be text or ndjson")
        self.format = format
        self.lock = threading.Lock()
        self.owns_stream = destination != "-"
        if not self.owns_stream and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        self.stream = Path(destination).expanduser().open("x", encoding="utf-8", buffering=1) if self.owns_stream else sys.stdout

    def emit(self, event, message, **data):
        timestamp = dt.datetime.now(dt.timezone.utc).isoformat()
        if self.format == "ndjson":
            line = json.dumps({"schema_version": "1.0", "timestamp": timestamp, "event": event, "message": message, **data}, allow_nan=False)
        else:
            # A caller-controlled model name must not inject fake progress lines.
            line = f"[{timestamp}] {event}: " + str(message).replace("\r", " ").replace("\n", " ")
        with self.lock:
            self.stream.write(line + "\n")
            self.stream.flush()

    def close(self):
        if self.owns_stream:
            self.stream.close()
