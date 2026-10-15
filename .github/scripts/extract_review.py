"""Extract the final assistant answer, excluding OpenCode's tool output."""

import json
from pathlib import Path
import sys


def extract_review(events):
    if any(event.get("type") == "error" for event in events):
        raise ValueError("OpenCode reported an error; refusing to infer approval")
    parts = [event["part"] for event in events
             if event.get("type") == "text" and isinstance(event.get("part", {}).get("text"), str)]
    if not parts:
        raise ValueError("OpenCode produced no assistant review text")
    final_message = parts[-1].get("messageID")
    answer = "\n".join(part["text"] for part in parts if part.get("messageID") == final_message).strip()
    if not answer or answer.splitlines()[0] not in {
        "REVIEW_VERDICT: APPROVED", "REVIEW_VERDICT: CHANGES_REQUESTED"
    }:
        raise ValueError("Final assistant answer needs a valid review verdict")
    return answer + "\n"


if __name__ == "__main__":
    events = [json.loads(line) for line in Path(sys.argv[1]).read_text().splitlines() if line.strip()]
    Path(sys.argv[2]).write_text(extract_review(events))
