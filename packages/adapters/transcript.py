import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class TranscriptLogger:
    """Appends one JSON line per turn to <workspace_dir>/<filename> so a
    different model can reconstruct context if the lead agent dies (see
    roadmap doc, '대화 기록(transcript) 보관')."""

    def __init__(self, workspace_dir: str, filename: str = ".ana_transcript.jsonl"):
        self.path = Path(workspace_dir) / filename

    def append(
        self,
        session_id: str,
        prompt: str,
        response: str,
        cost_usd: Optional[float] = None,
        error: Optional[str] = None,
    ) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "prompt": prompt,
            "response": response,
            "cost_usd": cost_usd,
        }
        # A failed turn's `response` is the notice the user saw; this is why it
        # failed. Kept apart so the notice stays readable and the cause stays
        # findable.
        if error:
            record["error"] = error
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
