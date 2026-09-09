import os
import re
import sys
import time
import base64
from pathlib import Path

import requests


# ============================================================
# CONFIGURATION
# ============================================================

GITHUB_OWNER = "yoami-dev"
GITHUB_REPO = "smm-trading-database-transfer"
GITHUB_BRANCH = "main"
GITHUB_FOLDER = "database-parts"

LOCAL_FOLDER = Path(
    r"D:\Milind\MyTradingPlatform\smm-trading-database-transfer"
)

# Keep the token OUT of this file.
# In Command Prompt:
#   set GITHUB_TOKEN=github_pat_xxxxxxxxx
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")

# Retry configuration
MAX_RETRIES = 8

# Initial delay for transient GitHub errors.
INITIAL_RETRY_DELAY = 30

# Maximum delay between retries.
MAX_RETRY_DELAY = 300

# Upload timeout for one 25 MB part.
UPLOAD_TIMEOUT = 900

# GET timeout.
GET_TIMEOUT = 60

# ============================================================
# END CONFIGURATION
# ============================================================


API_URL = "https://api.github.com"

HEADERS = {
    "Accept": "application/vnd.github+json",
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "X-GitHub-Api-Version": "2026-03-10",
    "User-Agent": "smm-trading-database-transfer",
}


def fail(message):
    print()
    print("=" * 70)
    print("ERROR")
    print("=" * 70)
    print(message)
    print("=" * 70)
    sys.exit(1)


def get_files():
    """
    Find files:

        smm_trading.7z.001
        smm_trading.7z.002
        ...
        smm_trading.7z.1382
    """

    pattern = re.compile(
        r"^smm_trading\.7z\.(\d+)$",
        re.IGNORECASE
    )

    files = []

    for path in LOCAL_FOLDER.iterdir():

        if not path.is_file():
            continue

        match = pattern.match(path.name)

        if match:
            part_number = int(match.group(1))
            files.append((part_number, path))

    files.sort(key=lambda x: x[0])

    return files


def check_parts(files):

    if not files:
        fail(
            f"No split files found in:\n{LOCAL_FOLDER}"
        )

    print()
    print(f"Found {len(files)} split files.")

    numbers = [x[0] for x in files]

    expected = list(range(1, max(numbers) + 1))

    missing = sorted(set(expected) - set(numbers))

    if missing:

        print()
        print("MISSING PARTS:")
        print()

        for number in missing:
            print(
                f"  smm_trading.7z.{number:03d}"
            )

        fail(
            "One or more split files are missing. "
            "Do not start the upload."
        )

    duplicates = [
        number
        for number in set(numbers)
        if numbers.count(number) > 1
    ]

    if duplicates:
        fail(
            "Duplicate split-part numbers detected: "
            + ", ".join(
                f"{n:03d}" for n in duplicates
            )
        )

    print(
        f"Parts detected: 001 -> {max(numbers):03d}"
    )


def get_repo_info():

    url = (
        f"{API_URL}/repos/"
        f"{GITHUB_OWNER}/"
        f"{GITHUB_REPO}"
    )

    response = requests.get(
        url,
        headers=HEADERS,
        timeout=GET_TIMEOUT
    )

    if response.status_code == 200:
        return response.json()

    if response.status_code == 404:
        fail(
            f"Repository not found:\n"
            f"https://github.com/"
            f"{GITHUB_OWNER}/"
            f"{GITHUB_REPO}\n\n"
            f"Check GITHUB_OWNER and GITHUB_REPO."
        )

    if response.status_code in (401, 403):
        fail(
            "GitHub authentication/permission check failed.\n"
            "Confirm that the token has Contents: Read and write "
            "permission for this repository.\n\n"
            f"HTTP {response.status_code}\n"
            f"{response.text}"
        )

    fail(
        f"Unable to access repository.\n"
        f"HTTP {response.status_code}\n"
        f"{response.text}"
    )


def get_remote_file(path):

    url = (
        f"{API_URL}/repos/"
        f"{GITHUB_OWNER}/"
        f"{GITHUB_REPO}/"
        f"contents/{path}"
    )

    response = requests.get(
        url,
        headers=HEADERS,
        params={"ref": GITHUB_BRANCH},
        timeout=GET_TIMEOUT
    )

    if response.status_code == 200:
        return response.json()

    if response.status_code == 404:
        return None

    raise RuntimeError(
        f"GitHub GET failed: "
        f"{response.status_code} "
        f"{response.text}"
    )


def retry_delay(response, attempt):
    """
    Determine how long to wait before retrying.

    GitHub recommends:
    - honor Retry-After when supplied
    - honor x-ratelimit-reset when remaining == 0
    - otherwise use at least one minute for rate-limit situations
      and exponential backoff.
    """

    retry_after = response.headers.get("Retry-After")

    if retry_after:
        try:
            return max(
                1,
                int(float(retry_after))
            )
        except ValueError:
            pass

    remaining = response.headers.get(
        "X-RateLimit-Remaining"
    )

    reset = response.headers.get(
        "X-RateLimit-Reset"
    )

    if remaining == "0" and reset:
        try:
            seconds = int(reset) - int(time.time())
            if seconds > 0:
                return min(
                    max(seconds + 5, 60),
                    MAX_RETRY_DELAY
                )
        except ValueError:
            pass

    delay = INITIAL_RETRY_DELAY * (
        2 ** (attempt - 1)
    )

    return min(
        delay,
        MAX_RETRY_DELAY
    )


def is_retryable(response):

    status = response.status_code
    body = response.text.lower()

    # Normal transient HTTP/API errors.
    if status in (
        408,
        409,
        429,
        500,
        502,
        503,
        504
    ):
        return True

    # GitHub can return 403 for secondary rate limits
    # or rule-validation timeouts.
    if status == 403:

        retry_words = (
            "timed out validating rule",
            "secondary rate limit",
            "rate limit",
            "abuse detection",
            "temporarily blocked",
            "please try again later"
        )

        return any(
            word in body
            for word in retry_words
        )

    return False


def print_rate_headers(response):

    remaining = response.headers.get(
        "X-RateLimit-Remaining"
    )

    reset = response.headers.get(
        "X-RateLimit-Reset"
    )

    if remaining is not None:
        print(
            f"GitHub API remaining: {remaining}"
        )

    if reset:
        try:
            reset_time = time.strftime(
                "%Y-%m-%d %H:%M:%S",
                time.localtime(int(reset))
            )
            print(
                f"Rate-limit reset: {reset_time}"
            )
        except ValueError:
            pass


def upload_file(local_path, remote_path):

    file_size = local_path.stat().st_size

    print()
    print("-" * 70)
    print("Uploading:")
    print(f"  {local_path.name}")
    print(
        f"  Size: "
        f"{file_size / (1024 * 1024):.2f} MB"
    )
    print("-" * 70)

    url = (
        f"{API_URL}/repos/"
        f"{GITHUB_OWNER}/"
        f"{GITHUB_REPO}/"
        f"contents/{remote_path}"
    )

    # Check whether this part is already on GitHub.
    # This makes the script safely resumable.
    remote = get_remote_file(remote_path)

    if remote:

        print("Remote file already exists.")

        remote_size = remote.get("size", -1)

        if remote_size == file_size:
            print(
                "Size matches. "
                "Skipping upload."
            )
            return True

        print(
            "Remote file exists but size differs."
        )

        sha = remote.get("sha")

    else:
        sha = None

    # Read the local file once.
    # A 25 MB part becomes about 33.3 MB after Base64 encoding.
    try:
        with open(local_path, "rb") as f:
            raw_data = f.read()
    except OSError as e:
        print(f"Unable to read file: {e}")
        return False

    encoded = base64.b64encode(
        raw_data
    ).decode("ascii")

    for attempt in range(
        1,
        MAX_RETRIES + 1
    ):

        try:

            print(
                f"Attempt {attempt}/"
                f"{MAX_RETRIES}"
            )

            payload = {
                "message":
                    f"Upload {local_path.name}",
                "content":
                    encoded,
                "branch":
                    GITHUB_BRANCH
            }

            if sha:
                payload["sha"] = sha

            response = requests.put(
                url,
                headers=HEADERS,
                json=payload,
                timeout=UPLOAD_TIMEOUT
            )

            if response.status_code in (
                200,
                201
            ):

                print(
                    "SUCCESS: Uploaded."
                )

                return True

            print_rate_headers(response)

            if is_retryable(response):

                delay = retry_delay(
                    response,
                    attempt
                )

                print()
                print(
                    f"Temporary GitHub error "
                    f"{response.status_code}:"
                )
                print(response.text)

                # If GitHub may have completed the operation but
                # the response was lost/blocked, check before retrying.
                try:
                    remote_after_error = get_remote_file(
                        remote_path
                    )

                    if remote_after_error:
                        remote_size = (
                            remote_after_error.get(
                                "size",
                                -1
                            )
                        )

                        if remote_size == file_size:
                            print(
                                "Remote file is present "
                                "with the expected size."
                            )
                            print(
                                "Treating this part as successful."
                            )
                            return True

                        sha = remote_after_error.get(
                            "sha"
                        )

                except Exception as check_error:
                    print(
                        "Could not verify remote state:"
                    )
                    print(check_error)

                if attempt < MAX_RETRIES:

                    print(
                        f"Waiting {delay} seconds "
                        f"before retry..."
                    )

                    time.sleep(delay)
                    continue

            print()
            print(
                "GitHub returned a non-retryable error:"
            )
            print(
                f"HTTP {response.status_code}"
            )
            print(response.text)

            return False

        except requests.Timeout as e:

            print(
                f"Upload timeout: {e}"
            )

            if attempt < MAX_RETRIES:

                delay = min(
                    INITIAL_RETRY_DELAY *
                    (2 ** (attempt - 1)),
                    MAX_RETRY_DELAY
                )

                print(
                    f"Waiting {delay} seconds "
                    f"before retry..."
                )

                time.sleep(delay)
                continue

            return False

        except requests.RequestException as e:

            print(
                f"Network error: {e}"
            )

            if attempt < MAX_RETRIES:

                delay = min(
                    INITIAL_RETRY_DELAY *
                    (2 ** (attempt - 1)),
                    MAX_RETRY_DELAY
                )

                print(
                    f"Waiting {delay} seconds "
                    f"before retry..."
                )

                time.sleep(delay)
                continue

            return False

        except Exception as e:

            print(
                f"Unexpected error: {e}"
            )

            return False

    return False


def main():

    print()
    print("=" * 70)
    print("SMM TRADING DATABASE - GITHUB UPLOADER")
    print("=" * 70)

    # --------------------------------------------------------
    # Validate token
    # --------------------------------------------------------

    if not GITHUB_TOKEN:

        fail(
            "GITHUB_TOKEN environment variable "
            "is not set.\n\n"
            "Run:\n"
            "set GITHUB_TOKEN=github_pat_YOUR_TOKEN"
        )

    # --------------------------------------------------------
    # Validate local folder
    # --------------------------------------------------------

    if not LOCAL_FOLDER.exists():

        fail(
            f"Folder does not exist:\n"
            f"{LOCAL_FOLDER}"
        )

    # --------------------------------------------------------
    # Get split files
    # --------------------------------------------------------

    files = get_files()

    check_parts(files)

    # --------------------------------------------------------
    # Calculate total size
    # --------------------------------------------------------

    total_size = sum(
        path.stat().st_size
        for _, path in files
    )

    print()
    print(
        f"Total size: "
        f"{total_size / (1024**3):.2f} GB"
    )

    # --------------------------------------------------------
    # Test GitHub connection
    # --------------------------------------------------------

    print()
    print("Checking GitHub repository...")

    repo = get_repo_info()

    print(
        f"Connected to: "
        f"{repo['full_name']}"
    )

    # --------------------------------------------------------
    # Confirm before starting
    # --------------------------------------------------------

    print()
    print("IMPORTANT:")
    print(
        "This will upload approximately "
        f"{total_size / (1024**3):.2f} GB "
        f"using {len(files)} files."
    )

    print()
    print(
        "The script will automatically retry "
        "temporary GitHub/rate-limit/rule-validation "
        "errors with exponential backoff."
    )

    print(
        "It will also resume safely because already "
        "uploaded parts are skipped."
    )

    answer = input(
        "\nType YES to start/resume upload: "
    )

    if answer.strip().upper() != "YES":

        print(
            "Upload cancelled."
        )

        return

    # --------------------------------------------------------
    # Upload sequentially
    # --------------------------------------------------------

    start_time = time.time()

    successful = 0
    failed = []

    for index, (
        part_number,
        local_path
    ) in enumerate(
        files,
        start=1
    ):

        remote_path = (
            f"{GITHUB_FOLDER}/"
            f"{local_path.name}"
        )

        print()
        print(
            f"[{index}/{len(files)}] "
            f"PART {part_number:03d}"
        )

        success = upload_file(
            local_path,
            remote_path
        )

        if success:

            successful += 1

        else:

            failed.append(
                local_path.name
            )

            print()
            print(
                "UPLOAD FAILED."
            )

            print(
                "Stopping so you can investigate."
            )

            break

        elapsed = (
            time.time() -
            start_time
        )

        if successful:

            average = (
                elapsed /
                successful
            )

            remaining = (
                len(files) -
                successful
            )

            eta = (
                average *
                remaining
            )

            print(
                f"Progress: "
                f"{successful}/"
                f"{len(files)}"
            )

            print(
                f"Estimated remaining: "
                f"{eta / 60:.1f} minutes"
            )

    # --------------------------------------------------------
    # Final result
    # --------------------------------------------------------

    elapsed = (
        time.time() -
        start_time
    )

    print()
    print("=" * 70)
    print("UPLOAD SUMMARY")
    print("=" * 70)

    print(
        f"Successful: "
        f"{successful}/{len(files)}"
    )

    print(
        f"Time: "
        f"{elapsed / 3600:.2f} hours"
    )

    if failed:

        print()
        print("FAILED FILES:")

        for filename in failed:
            print(
                f"  {filename}"
            )

        print()
        print(
            "Run the script again to resume."
        )

    else:

        print()
        print(
            "ALL FILES UPLOADED SUCCESSFULLY."
        )

        print()
        print(
            "GitHub folder:"
        )

        print(
            f"https://github.com/"
            f"{GITHUB_OWNER}/"
            f"{GITHUB_REPO}/tree/"
            f"{GITHUB_BRANCH}/"
            f"{GITHUB_FOLDER}"
        )

    print("=" * 70)


if __name__ == "__main__":
    main()
