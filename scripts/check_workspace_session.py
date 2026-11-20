"""Record a synthetic rejected write and its owner-visible history through the public client."""

import argparse
import hashlib
import json
from pathlib import Path
import secrets
import shutil
import tempfile
import uuid

import httpx

from client.cli import Client
from client.mounted import MountedSession


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    client = Client(args.url)
    client.preflight("mock")
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "project"
        fixture = next(Path("baf/B4-edit").glob("B4.1-*/fixture"))
        shutil.copytree(fixture, folder)
        original = folder / "README.md"
        before = digest(original)
        external = uuid.uuid4().hex
        instruction = "Update the installation version. [replay:B4.1]"
        with MountedSession.open(client, folder) as session:
            run = session.submit(instruction, request_id=external)
            state, trace = session.follow(run)
            assert state.get("error") == "write_unverified" and digest(original) == before
            (args.output / "active.jsonl").write_text("".join(json.dumps(row) + "\n" for row in trace))
            decision = next(row["decision"] for row in trace if row["type"] == "write_decision")
            proposal = session.save_proposal(decision["proposal_id"], Path(tmp) / "export")
            assert "==1.3" in proposal.read_text() and digest(proposal) != before
            stranger = client.http.get(f"/runs/{run}", headers={"X-Session-Key": secrets.token_urlsafe(32)})
            assert stranger.status_code == 403
            cursor = trace[0]["seq"]
            assert session.events(run, cursor) == trace[1:]
        try:
            session.submit(instruction, request_id=external)
        except httpx.HTTPStatusError as exc:
            replay_status = exc.response.status_code
        else:
            raise AssertionError("A closed session accepted a command")
        assert replay_status == 409
        history = session.history()
        rows = [json.loads(line) for line in history.splitlines()]
        assert {row["source"] for row in rows} == {"harness", "sandbox"}
        assert all(text not in history for text in ("README.md", "==1.3", "RunCommand already", "write_text"))
        (args.output / "closed.jsonl").write_text(history)
        result = {"mode": "mock", "case": "B4.1 with uncoordinated editor", "state": state["error"],
                  "original_before_sha256": before, "original_after_sha256": digest(original),
                  "exported_proposal_sha256": digest(proposal), "wrong_owner_http": stranger.status_code,
                  "duplicate_after_release_http": replay_status, "resume_after": cursor,
                  "active_events": len(trace), "technical_events": len(rows)}
        (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))


if __name__ == "__main__":
    main()
