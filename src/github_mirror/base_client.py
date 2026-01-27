"""shared retry and download logic for api clients"""

import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from .log import log
from .models import Repository


@dataclass
class RateLimitInfo:
    limit: int
    remaining: int
    used: int

MAX_RETRIES = 3
RETRY_STATUS_CODES = {500, 502, 503, 504}


class RateLimitError(Exception):
    def __init__(self, reset_time: int):
        self.reset_time = reset_time
        super().__init__(f"rate limit exceeded, resets at {reset_time}")


class BaseClient:
    RATE_LIMIT_THRESHOLD = 10
    RATE_LIMIT_REMAINING_HEADER = "X-RateLimit-Remaining"
    RATE_LIMIT_RESET_HEADER = "X-RateLimit-Reset"

    def __init__(self, proxy: str | None = None):
        self.session = requests.Session()
        self.request_count = 0

        if proxy:
            self.session.proxies = {
                "http": f"http://{proxy}",
                "https": f"http://{proxy}",
            }

    def _check_rate_limit(self, response: requests.Response) -> None:
        remaining = response.headers.get(self.RATE_LIMIT_REMAINING_HEADER)
        reset_time = response.headers.get(self.RATE_LIMIT_RESET_HEADER)

        if remaining is None:
            return

        remaining_int = int(remaining)
        log(f"rate limit remaining: {remaining_int}", "debug")

        if remaining_int < self.RATE_LIMIT_THRESHOLD and reset_time:
            reset_timestamp = int(reset_time)
            wait_seconds = max(0, reset_timestamp - int(time.time())) + 1
            log(
                f"rate limit low ({remaining_int}), waiting {wait_seconds}s...",
                "warning",
            )
            time.sleep(wait_seconds)

    def _handle_error_response(self, response: requests.Response) -> None:
        """handle non-200 responses, override for platform-specific codes"""
        if response.status_code == 403:
            remaining = response.headers.get(self.RATE_LIMIT_REMAINING_HEADER)
            reset_time = response.headers.get(self.RATE_LIMIT_RESET_HEADER)
            if remaining == "0" and reset_time:
                raise RateLimitError(int(reset_time))
            raise PermissionError(response.text)

        if response.status_code == 404:
            raise FileNotFoundError(response.url)

        if response.status_code in RETRY_STATUS_CODES:
            raise ConnectionError(f"server error {response.status_code}")

        if response.status_code != 200:
            raise RuntimeError(f"api error {response.status_code}: {response.text}")

    def _request(self, url: str) -> Any:
        log(f"GET {url}", "debug")
        self.request_count += 1

        try:
            response = self.session.get(url, timeout=30)
        except requests.exceptions.ProxyError as e:
            raise ConnectionError(f"proxy error: {e}") from e
        except requests.exceptions.RequestException as e:
            raise ConnectionError(e) from e

        self._check_rate_limit(response)
        self._handle_error_response(response)

        return response.json()

    def _request_with_retry(self, url: str) -> Any:
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                return self._request(url)
            except ConnectionError as e:
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    delay = (2**attempt) + random.uniform(0, 1)
                    log(f"retry {attempt + 1}/{MAX_RETRIES} in {delay:.1f}s: {e}", "warning")
                    time.sleep(delay)
        raise last_error  # type: ignore[misc]

    def get_release_count(self, repo: Repository) -> int:
        """get total release count via Link header (1 api request)"""
        url = f"{repo.get_releases_url()}?per_page=1"
        log(f"HEAD {url}", "debug")
        self.request_count += 1

        try:
            response = self.session.get(url, timeout=30)
        except requests.exceptions.RequestException as e:
            raise ConnectionError(e) from e

        if response.status_code == 404:
            raise FileNotFoundError(url)
        if response.status_code != 200:
            raise RuntimeError(f"api error {response.status_code}")

        link = response.headers.get("Link", "")
        match = re.search(r'[?&]page=(\d+)[^>]*>;\s*rel="last"', link)
        if match:
            return int(match.group(1))
        return len(response.json())

    def _download_once(
        self,
        url: str,
        dest: Path,
        expected_size: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        try:
            response = self.session.get(url, stream=True, timeout=300, headers=headers)
            if response.status_code in RETRY_STATUS_CODES:
                raise ConnectionError(f"server error {response.status_code}")
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            raise ConnectionError(e) from e

        total = int(response.headers.get("content-length", 0)) or expected_size or 0
        downloaded = 0
        last_logged_pct = -10

        with open(dest, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
                downloaded += len(chunk)

                if total:
                    pct = int((downloaded / total) * 100)
                    if pct >= last_logged_pct + 10:
                        log(f"downloading: {pct}%", "debug")
                        last_logged_pct = pct

    def download_file(
        self, url: str, dest: Path, expected_size: int | None = None
    ) -> None:
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                return self._download_once(url, dest, expected_size)
            except ConnectionError as e:
                last_error = e
                if dest.exists():
                    dest.unlink()
                if attempt < MAX_RETRIES - 1:
                    delay = (2**attempt) + random.uniform(0, 1)
                    log(f"download retry {attempt + 1}/{MAX_RETRIES} in {delay:.1f}s", "warning")
                    time.sleep(delay)
        raise last_error  # type: ignore[misc]
