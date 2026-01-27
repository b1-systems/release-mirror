import requests

from .base_client import BaseClient, RateLimitError, RateLimitInfo
from .models import Release, Repository

__all__ = ["GitLabClient", "RateLimitError"]


class GitLabClient(BaseClient):
    # gitlab uses different header names (no X- prefix)
    RATE_LIMIT_REMAINING_HEADER = "RateLimit-Remaining"
    RATE_LIMIT_RESET_HEADER = "RateLimit-Reset"

    def __init__(self, proxy: str | None = None, token: str | None = None, base_url: str = "https://gitlab.com"):
        super().__init__(proxy)
        self.base_url = base_url

        self.session.headers["User-Agent"] = "release-mirror/0.8.0"

        if token:
            self.session.headers["PRIVATE-TOKEN"] = token

    def get_rate_limit(self) -> RateLimitInfo | None:
        """get rate limit from headers (makes a lightweight api request)"""
        # gitlab has no dedicated rate_limit endpoint, use /version as lightweight probe
        try:
            response = self.session.get(f"{self.base_url}/api/v4/version", timeout=10)
        except requests.exceptions.RequestException:
            return None

        limit = response.headers.get("RateLimit-Limit")
        remaining = response.headers.get("RateLimit-Remaining")

        if limit is None or remaining is None:
            return None  # rate limiting may not be enabled

        return RateLimitInfo(
            limit=int(limit),
            remaining=int(remaining),
            used=int(limit) - int(remaining),
        )

    def _handle_error_response(self, response: requests.Response) -> None:
        # gitlab returns 401 for auth errors
        if response.status_code == 401:
            raise PermissionError("authentication required")
        super()._handle_error_response(response)

    def get_releases(self, repo: Repository, per_page: int = 100) -> list[Release]:
        releases: list[Release] = []
        page = 1

        while True:
            url = f"{repo.get_releases_url()}?per_page={per_page}&page={page}"
            data = self._request_with_retry(url)

            if not data:
                break

            for item in data:
                releases.append(Release.from_gitlab_api(item))

            if len(data) < per_page:
                break

            page += 1

        return releases
