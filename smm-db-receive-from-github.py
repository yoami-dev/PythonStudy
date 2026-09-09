#!/usr/bin/env python3
"""
SMM Trading Database - GitHub Part Receiver

Downloads split 7-Zip parts from GitHub to a local folder and supports resume.
After all parts are downloaded, the script verifies that every part is present.

Requirements:
    pip install requests

GitHub token:
    Set the same GITHUB_TOKEN environment variable used by the upload script.

Windows example:
    set GITHUB_TOKEN=YOUR_TOKEN
    python smm-db-receive-from-github.py

The script downloads:
    database-parts/smm_trading.7z.001
    ...
    database-parts/smm_trading.7z.1382

It does NOT automatically extract the database.
After all parts are downloaded, extract smm_trading.7z.001 with 7-Zip.
"""

import os
import re
import sys
import time
from pathlib import Path

import requests


# ============================================================
# CONFIGURATION
# ============================================================

GITHUB_OWNER = "yoami-dev"
GITHUB_REPO = "smm-trading-database-transfer"
GITHUB_BRANCH = "main"
GITHUB_FOLDER = "database-parts"

# Change this to the desired folder on the OFFICE PC.
LOCAL_FOLDER = Path(r"D:\Milind\MyTradingPlatform\smm-trading-database-transfer")

EXPECTED_PARTS = 1382

# GitHub API settings
MAX_RETRIES = 8
INITIAL_RETRY_DELAY = 10
MAX_RETRY_DELAY = 300
DOWNLOAD_TIMEOUT = 900

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")

API_URL = (
    f"https://api.github.com/repos/"
    f"{GITHUB_OWNER}/{GITHUB_REPO}/contents/{GITHUB_FOLDER}"
)

HEADERS = {
    "Accept": "application/vnd.github+json",
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "smm-trading-database-transfer-receiver",
}


# ============================================================
# HELPERS
# ============================================================

def fail(message):
    print()
    print("ERROR:")
    print(message)
    sys.exit(1)


def retry_delay(response=None, attempt=1):
    """Choose a safe retry delay using GitHub headers where available."""
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(MAX_RETRY_DELAY, max(5, int(float(retry_after))))
            except ValueError:
                pass

        remaining = response.headers.get("X-RateLimit-Remaining")
        reset = response.headers.get("X-RateLimit-Reset")

        if remaining == "0" and reset:
            try:
                wait = int(reset) - int(time.time()) + 5
                return min(MAX_RETRY_DELAY, max(10, wait))
            except ValueError:
                pass

    return min(MAX_RETRY_DELAY, INITIAL_RETRY_DELAY * (2 ** (attempt - 1)))


def is_retryable(response):
    """Identify temporary GitHub/API errors."""
    if response.status_code in (408, 409, 429, 500, 502, 503, 504):
        return True

    if response.status_code == 403:
        text = response.text.lower()
        temporary_phrases = (
            "timed out validating rule",
            "secondary rate limit",
            "rate limit",
            "abuse detection",
            "temporarily blocked",
            "please try again later",
        )
        return any(p in text for p in temporary_phrases)

    return False


def github_error(response):
    try:
        data = response.json()
        return data.get("message", response.text[:500])
    except Exception:
        return response.text[:500]


def get_remote_parts():
    """
    Get all files in the GitHub database-parts folder.
    Returns {filename: metadata}.
    """
    url = API_URL
    params = {"ref": GITHUB_BRANCH}
    remote = {}

    while url:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = requests.get(
                    url,
                    headers=HEADERS,
                    params=params,
                    timeout=60,
                )

                if response.status_code == 200:
                    items = response.json()

                    for item in items:
                        if item.get("type") == "file":
                            remote[item["name"]] = item

                    # GitHub API pagination can provide a next URL in Link.
                    next_url = None
                    link = response.headers.get("Link", "")
                    for section in link.split(","):
                        if 'rel="next"' in section:
                            next_url = section.split(";")[0].strip().strip("<>")
                            break

                    url = next_url
                    params = None
                    break

                if is_retryable(response) and attempt < MAX_RETRIES:
                    delay = retry_delay(response, attempt)
                    print(
                        f"  GitHub returned HTTP {response.status_code}. "
                        f"Retrying in {delay} seconds..."
                    )
                    time.sleep(delay)
                    continue

                fail(
                    f"Could not read GitHub folder.\n"
                    f"HTTP {response.status_code}: {github_error(response)}"
                )

            except requests.RequestException as exc:
                if attempt >= MAX_RETRIES:
                    fail(f"Network error while reading GitHub: {exc}")

                delay = retry_delay(attempt=attempt)
                print(f"  Network error: {exc}")
                print(f"  Retrying in {delay} seconds...")
                time.sleep(delay)

    return remote


def download_part(filename, remote_info, destination):
    """
    Download one GitHub part using the API's download URL.
    Uses a temporary .part file so an interrupted download is never
    mistaken for a complete file.
    """
    expected_size = remote_info.get("size")

    # Already complete.
    if destination.exists():
        actual_size = destination.stat().st_size
        if expected_size is None or actual_size == expected_size:
            return "skipped"

        print(
            f"  Existing file has wrong size "
            f"({actual_size:,} bytes; expected {expected_size:,})."
        )
        destination.unlink()

    download_url = remote_info.get("download_url")
    if not download_url:
        fail(f"No download URL available for {filename}.")

    temp_file = destination.with_name(destination.name + ".part")

    # Remove an old partial download because GitHub's download URL does not
    # provide a reliable resume contract for this workflow.
    if temp_file.exists():
        temp_file.unlink()

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with requests.get(
                download_url,
                headers=HEADERS,
                stream=True,
                timeout=DOWNLOAD_TIMEOUT,
            ) as response:

                if response.status_code != 200:
                    if is_retryable(response) and attempt < MAX_RETRIES:
                        delay = retry_delay(response, attempt)
                        print(
                            f"  Download HTTP {response.status_code}. "
                            f"Retrying in {delay} seconds..."
                        )
                        time.sleep(delay)
                        continue

                    fail(
                        f"Failed downloading {filename}.\n"
                        f"HTTP {response.status_code}: "
                        f"{github_error(response)}"
                    )

                total = int(response.headers.get("Content-Length", 0))
                downloaded = 0
                last_report = 0

                with open(temp_file, "wb") as f:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue

                        f.write(chunk)
                        downloaded += len(chunk)

                        # Progress approximately every 5%.
                        if total:
                            percent = int(downloaded * 100 / total)
                            if percent >= last_report + 5 or percent == 100:
                                print(
                                    f"\r    {percent:3d}% "
                                    f"({downloaded / 1024 / 1024:.1f} / "
                                    f"{total / 1024 / 1024:.1f} MB)",
                                    end="",
                                    flush=True,
                                )
                                last_report = percent

                print()

            actual_size = temp_file.stat().st_size

            if expected_size is not None and actual_size != expected_size:
                temp_file.unlink(missing_ok=True)

                if attempt < MAX_RETRIES:
                    delay = retry_delay(attempt=attempt)
                    print(
                        f"  Size mismatch for {filename}. "
                        f"Retrying in {delay} seconds..."
                    )
                    time.sleep(delay)
                    continue

                fail(
                    f"Size verification failed for {filename}: "
                    f"downloaded {actual_size:,}, "
                    f"expected {expected_size:,} bytes."
                )

            temp_file.replace(destination)
            return "downloaded"

        except requests.RequestException as exc:
            temp_file.unlink(missing_ok=True)

            if attempt >= MAX_RETRIES:
                fail(f"Network error downloading {filename}: {exc}")

            delay = retry_delay(attempt=attempt)
            print(f"  Network error: {exc}")
            print(f"  Retrying in {delay} seconds...")
            time.sleep(delay)

        except OSError as exc:
            temp_file.unlink(missing_ok=True)

            if attempt >= MAX_RETRIES:
                fail(f"Disk/file error for {filename}: {exc}")

            delay = retry_delay(attempt=attempt)
            print(f"  File error: {exc}")
            print(f"  Retrying in {delay} seconds...")
            time.sleep(delay)

    fail(f"Could not download {filename}.")


def verify_local_parts():
    """Verify that all expected numbered parts exist."""
    missing = []
    wrong_size = []

    for number in range(1, EXPECTED_PARTS + 1):
        filename = f"smm_trading.7z.{number:03d}"
        path = LOCAL_FOLDER / filename

        if not path.exists():
            missing.append(filename)

    if missing:
        return False, missing, wrong_size

    return True, missing, wrong_size


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 72)
    print(" SMM TRADING DATABASE - GITHUB RECEIVER")
    print("=" * 72)
    print()
    print(f"GitHub : {GITHUB_OWNER}/{GITHUB_REPO}")
    print(f"Branch : {GITHUB_BRANCH}")
    print(f"Folder : {GITHUB_FOLDER}")
    print(f"Local  : {LOCAL_FOLDER}")
    print(f"Parts  : {EXPECTED_PARTS}")
    print()

    if not GITHUB_TOKEN:
        fail(
            "GITHUB_TOKEN environment variable is not set.\n\n"
            "Set it first, for example:\n"
            "  set GITHUB_TOKEN=YOUR_TOKEN\n\n"
            "Do not put the token directly into this script."
        )

    LOCAL_FOLDER.mkdir(parents=True, exist_ok=True)

    print("Checking GitHub for database parts...")
    remote = get_remote_parts()

    pattern = re.compile(r"^smm_trading\.7z\.(\d+)$")

    parts = []
    for filename, info in remote.items():
        match = pattern.match(filename)
        if match:
            parts.append((int(match.group(1)), filename, info))

    parts.sort(key=lambda x: x[0])

    print(f"Found {len(parts):,} database part(s) on GitHub.")

    if not parts:
        fail("No smm_trading.7z.xxx parts were found on GitHub.")

    numbers = {number for number, _, _ in parts}
    missing_remote = [
        number for number in range(1, EXPECTED_PARTS + 1)
        if number not in numbers
    ]

    if missing_remote:
        print()
        print(
            f"WARNING: {len(missing_remote)} expected part(s) are "
            f"not currently on GitHub."
        )

        preview = ", ".join(f"{n:03d}" for n in missing_remote[:20])
        print(f"Missing: {preview}")

        if len(missing_remote) > 20:
            print("...")

        print()
        print("The script will download all parts that are available.")
        print("You can run it again later after the missing parts are uploaded.")

    total_bytes = sum(info.get("size", 0) for _, _, info in parts)
    print(f"Remote size: {total_bytes / (1024**3):.2f} GB")
    print()

    answer = input("Type YES to start/resume download: ").strip()
    if answer != "YES":
        print("Cancelled.")
        return

    print()
    print("Starting download...")
    print()

    downloaded_count = 0
    skipped_count = 0
    failed_count = 0

    for index, (number, filename, info) in enumerate(parts, start=1):
        destination = LOCAL_FOLDER / filename

        print(
            f"[{index}/{len(parts)}] Part {number:03d} - {filename}"
        )

        try:
            result = download_part(filename, info, destination)

            if result == "skipped":
                skipped_count += 1
                print("  Already complete - skipped.")
            else:
                downloaded_count += 1
                print("  Downloaded successfully.")

        except KeyboardInterrupt:
            print()
            print("Download stopped by user.")
            print("Run the script again to resume.")
            return

        print()

    print("=" * 72)
    print(" DOWNLOAD SUMMARY")
    print("=" * 72)
    print(f"Downloaded : {downloaded_count:,}")
    print(f"Skipped    : {skipped_count:,}")
    print(f"Remote     : {len(parts):,}")
    print()

    complete, missing, _ = verify_local_parts()

    if complete:
        print("SUCCESS: All 1,382 parts are present locally.")
        print()
        print("Next step:")
        print("1. Open the folder in Windows Explorer.")
        print("2. Right-click smm_trading.7z.001.")
        print("3. Choose 7-Zip -> Extract Here")
        print("4. The original smm_trading.db should be reconstructed.")
        print()
        print("IMPORTANT: Do not manually extract each .7z.xxx part.")
        print("Only start extraction from smm_trading.7z.001.")
    else:
        print(
            f"DOWNLOAD NOT COMPLETE: {len(missing):,} part(s) "
            f"are missing locally."
        )
        print("Run this script again after the remaining parts are available.")


if __name__ == "__main__":
    main()
