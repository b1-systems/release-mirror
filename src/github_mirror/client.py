from pathlib import Path

from . import __version__
from .base_client import BaseClient, RateLimitError, RateLimitInfo
from .models import Release, Repository

__all__ = ["GitHubClient", "RateLimitError"]


class GitHubClient(BaseClient):
    RATE_LIMIT_REMAINING_HEADER = "X-RateLimit-Remaining"
    RATE_LIMIT_RESET_HEADER = "X-RateLimit-Reset"

    def __init__(self, proxy: str | None = None, token: str | None = None):
        super().__init__(proxy)

        self.session.headers["Accept"] = "application/vnd.github+json"
        self.session.headers["User-Agent"] = f"release-mirror/{__version__}"

        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def get_rate_limit(self) -> RateLimitInfo:
        """get current rate limit status (1 api request, doesn't count against limit)"""
        data = self._request("https://api.github.com/rate_limit")
        rate = data["rate"]
        return RateLimitInfo(
            limit=rate["limit"],
            remaining=rate["remaining"],
            used=rate["used"],
        )

    def get_releases(self, repo: Repository, per_page: int = 100) -> list[Release]:
        releases: list[Release] = []
        page = 1

        while True:
            url = f"{repo.get_releases_url()}?per_page={per_page}&page={page}"
            data = self._request_with_retry(url)

            if not data:
                break

            for item in data:
                releases.append(Release.from_github_api(item))

            if len(data) < per_page:
                break

            page += 1

        return releases

    def _download_once(
        self,
        url: str,
        dest: Path,
        expected_size: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        # github needs Accept header for binary downloads, use per-request header
        dl_headers = {"Accept": "application/octet-stream"}
        if headers:
            dl_headers.update(headers)
        super()._download_once(url, dest, expected_size, headers=dl_headers)
