#!/usr/bin/env python3
"""Download every available corpus entry listed in manifest.json into raw/ and verify sha256.

Usage:
    python fetch_corpus.py                    # fetch missing files, verify everything
    python fetch_corpus.py --only id1,id2     # restrict to some entries
    python fetch_corpus.py --force            # re-download even if a verified file exists
    python fetch_corpus.py --update-manifest  # record sha256/bytes/retrieved_at for entries
                                              # whose manifest hash is still null

Behaviour:
  * Entries with status "unavailable" (or no url) are skipped.
  * Idempotent: a file already in raw/ whose sha256 matches the manifest is not re-downloaded.
  * Polite: fixed User-Agent, 1 s pause after every request, retries with backoff on
    429/5xx and network errors (honours Retry-After).
  * A download whose hash differs from the manifest is NOT written over raw/{id}.{type};
    it is saved as raw/{id}.{type}.mismatch for inspection and reported as a failure.

Exit status is 0 only if every downloadable entry ends up present and verified.
Dependencies: Python 3.8+ standard library and `requests`.
"""
import argparse
import datetime
import hashlib
import json
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "manifest.json"
RAW = HERE / "raw"

USER_AGENT = "SpinosaurusCorpusFetcher/0.1 (non-commercial research corpus)"
DELAY_S = 1.0
RETRIES = 5
MAX_BACKOFF_S = 120
TIMEOUT_S = 180
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_BYTES = 100 * 1000 * 1000  # corpus policy: no single document over 100 MB


class FetchError(Exception):
    pass


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def looks_valid(data, kind):
    """Cheap sanity check that we got the document and not an error/challenge page."""
    if kind == "pdf":
        return data[:5] == b"%PDF-"
    head = data[:4096].lower()
    return b"<html" in head or b"<!doctype html" in head


def download(session, url):
    last_err = "no attempt made"
    for attempt in range(1, RETRIES + 1):
        retry_after = None
        try:
            resp = session.get(url, timeout=TIMEOUT_S, allow_redirects=True)
        except requests.RequestException as e:
            last_err = f"{type(e).__name__}: {e}"
        else:
            if resp.status_code == 200:
                time.sleep(DELAY_S)
                return resp.content
            last_err = f"HTTP {resp.status_code}"
            if resp.status_code not in RETRY_STATUSES:
                time.sleep(DELAY_S)
                raise FetchError(last_err)
            retry_after = resp.headers.get("Retry-After")
        if attempt == RETRIES:
            break
        # Some hosts (e.g. bioRxiv) send "Retry-After: 0" while still rate-limiting,
        # so never wait less than the exponential backoff.
        wait = 10 * 2 ** (attempt - 1)
        if retry_after and retry_after.isdigit():
            wait = max(wait, int(retry_after))
        wait = min(wait, MAX_BACKOFF_S)
        print(f"    {last_err}; retry {attempt}/{RETRIES - 1} in {wait}s")
        time.sleep(wait)
    raise FetchError(f"{last_err} after {RETRIES} attempts")


def process(entry, session, force, update_manifest, today):
    """Return (outcome, message). outcome in {verified, downloaded, recorded, skipped, failed}."""
    eid, kind = entry["id"], entry["type"]
    if entry.get("status") != "ok" or not entry.get("url"):
        return "skipped", entry.get("status", "no url")

    target = RAW / f"{eid}.{kind}"
    expected = entry.get("sha256")

    if target.exists() and not force:
        actual = sha256_file(target)
        if expected and actual == expected:
            return "verified", "already present"
        if not expected and update_manifest:
            entry.update(sha256=actual, bytes=target.stat().st_size, retrieved_at=today)
            return "recorded", "hash recorded from existing file"
        # Present but unverifiable or mismatched: fall through and re-download.

    try:
        data = download(session, entry["url"])
    except FetchError as e:
        return "failed", str(e)

    if not looks_valid(data, kind):
        return "failed", f"response does not look like {kind} ({len(data)} bytes, starts {data[:16]!r})"
    if len(data) > MAX_BYTES:
        return "failed", f"{len(data):,} bytes exceeds the {MAX_BYTES:,}-byte corpus cap"

    actual = sha256_bytes(data)
    if expected and actual != expected:
        mismatch = target.with_name(target.name + ".mismatch")
        mismatch.write_bytes(data)
        return "failed", f"sha256 mismatch: manifest {expected[:12]}…, got {actual[:12]}… (saved {mismatch.name})"
    if not expected and not update_manifest:
        tmp = target.with_name(target.name + ".part")
        tmp.write_bytes(data)
        tmp.replace(target)
        return "failed", "downloaded but manifest has no sha256 (run with --update-manifest to record it)"

    tmp = target.with_name(target.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(target)
    if not expected:
        entry.update(sha256=actual, bytes=len(data), retrieved_at=today)
        return "recorded", f"downloaded {len(data):,} bytes, hash recorded"
    return "downloaded", f"downloaded {len(data):,} bytes, verified"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="comma-separated entry ids to process")
    ap.add_argument("--force", action="store_true", help="re-download even if a verified file exists")
    ap.add_argument("--update-manifest", action="store_true",
                    help="write sha256/bytes/retrieved_at into manifest.json for entries lacking a hash")
    args = ap.parse_args()

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    wanted = set(args.only.split(",")) if args.only else None
    if wanted:
        unknown = wanted - {e["id"] for e in manifest}
        if unknown:
            sys.exit(f"unknown ids: {', '.join(sorted(unknown))}")

    RAW.mkdir(exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()

    results = {}
    for entry in manifest:
        if wanted and entry["id"] not in wanted:
            continue
        outcome, msg = process(entry, session, args.force, args.update_manifest, today)
        results.setdefault(outcome, []).append(entry["id"])
        print(f"[{outcome:>10}] {entry['id']}: {msg}", flush=True)

    if args.update_manifest:
        MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"\nmanifest updated: {MANIFEST}")

    print("\nsummary: " + ", ".join(f"{k}={len(v)}" for k, v in sorted(results.items())))
    if results.get("failed"):
        print("failed: " + ", ".join(results["failed"]))
        sys.exit(1)


if __name__ == "__main__":
    main()
