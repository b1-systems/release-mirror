import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from .base_client import RateLimitError
from .client import GitHubClient
from .gitlab_client import GitLabClient
from .log import log
from .models import Asset, Config, Platform, Release, Repository

CHECKSUM_PATTERNS = (
    re.compile(r"checksums?\.txt$", re.IGNORECASE),
    re.compile(r"sha256sums?\.txt$", re.IGNORECASE),
    re.compile(r"sha256sums?$", re.IGNORECASE),
    re.compile(r"\.sha256(sum)?$", re.IGNORECASE),
)


def is_checksum_file(name: str) -> bool:
    """check if filename matches common checksum file patterns"""
    return any(p.search(name) for p in CHECKSUM_PATTERNS)


def parse_checksums(content: str, sidecar_name: str | None = None) -> dict[str, str]:
    """parse checksum file content, returns {filename: hash}

    sidecar_name: if provided and content is hash-only, infer target filename
    """
    checksums: dict[str, str] = {}

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # format: <hash>  <filename> or <hash> <filename>
        match = re.match(r"^([a-fA-F0-9]{64})\s+(.+)$", line)
        if match:
            hash_val, filename = match.groups()
                # strip leading path markers
            filename = re.sub(r"^[*./\\]+", "", filename)
            checksums[filename] = hash_val.lower()
            continue

        # single hash only (no filename) - infer from sidecar name
        if re.match(r"^[a-fA-F0-9]{64}$", line) and sidecar_name:
            target = re.sub(r"\.sha256(sum)?$", "", sidecar_name, flags=re.IGNORECASE)
            if target != sidecar_name:
                checksums[target] = line.lower()

    return checksums


class ReleaseClient(Protocol):
    request_count: int

    def get_releases(self, repo: Repository, per_page: int = 100) -> list[Release]: ...
    def get_release_count(self, repo: Repository) -> int: ...
    def download_file(
        self, url: str, dest: Path, expected_size: int | None = None
    ) -> None: ...


def create_client(platform: Platform, config: Config) -> ReleaseClient:
    if platform == Platform.GITHUB:
        return GitHubClient(proxy=config.proxy, token=config.github_token)
    elif platform == Platform.GITLAB:
        return GitLabClient(proxy=config.proxy, token=config.gitlab_token)
    raise ValueError(f"unsupported platform: {platform}")


class HashMismatchError(Exception):
    def __init__(self, path: Path, expected: str, actual: str):
        self.path = path
        self.expected = expected
        self.actual = actual
        super().__init__(f"hash mismatch for {path}: expected {expected}, got {actual}")


@dataclass
class MirrorResult:
    downloaded: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    hash_mismatches: list[HashMismatchError] = field(default_factory=list)
    api_requests: int = 0

    @property
    def success(self) -> bool:
        return not self.errors and not self.hash_mismatches


class ReleaseMirror:
    def __init__(self, config: Config, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run
        self._clients: dict[Platform, ReleaseClient] = {}

    def _get_client(self, platform: Platform) -> ReleaseClient:
        if platform not in self._clients:
            self._clients[platform] = create_client(platform, self.config)
        return self._clients[platform]

    def _compute_sha256(self, path: Path) -> str:
        sha256 = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256.update(chunk)
        return sha256.hexdigest()

    def _set_file_mtime(self, path: Path, dt: datetime) -> None:
        timestamp = dt.timestamp()
        os.utime(path, (timestamp, timestamp))

    def _update_latest_symlink(self, repo_path: Path, latest_tag: str) -> None:
        latest_link = repo_path / "latest"
        target = repo_path / latest_tag

        if not target.exists():
            return

        if latest_link.is_symlink() and latest_link.resolve() == target.resolve():
            return

        if latest_link.exists() and not latest_link.is_symlink():
            log(f"'latest' exists but is not a symlink: {latest_link}", "warning")
            return

        if self.dry_run:
            log(f"would create symlink: latest -> {latest_tag}", "debug")
            return

        temp_link = repo_path / f".latest.tmp.{os.getpid()}"
        try:
            temp_link.symlink_to(latest_tag)
            temp_link.rename(latest_link)
            log(f"updated latest -> {latest_tag}", "debug")
        except OSError:
            if temp_link.exists():
                temp_link.unlink()
            raise

    def _download_asset(
        self,
        asset: Asset,
        dest_dir: Path,
        result: MirrorResult,
        client: ReleaseClient,
        digest_override: str | None = None,
    ) -> None:
        dest_path = dest_dir / asset.name
        digest = digest_override or asset.digest

        if dest_path.exists():
            if digest:
                actual_hash = self._compute_sha256(dest_path)
                if actual_hash != digest:
                    err = HashMismatchError(dest_path, digest, actual_hash)
                    result.hash_mismatches.append(err)
                    log(f"hash mismatch: {asset.name}", "error")
                    return

            result.skipped.append(dest_path)
            log(f"skipped (exists): {asset.name}", "debug")
            return

        if self.dry_run:
            log(f"would download: {asset.name} ({asset.size} bytes)")
            return

        log(f"downloading: {asset.name}")

        try:
            client.download_file(asset.download_url, dest_path, asset.size)
        except ConnectionError as e:
            result.errors.append(f"download failed for {asset.name}: {e}")
            log(f"download failed: {asset.name} - {e}", "error")
            return

        if digest:
            actual_hash = self._compute_sha256(dest_path)
            if actual_hash != digest:
                err = HashMismatchError(dest_path, digest, actual_hash)
                result.hash_mismatches.append(err)
                log(f"hash mismatch after download: {asset.name}", "error")
                dest_path.unlink()
                return

        self._set_file_mtime(dest_path, asset.updated_at)
        result.downloaded.append(dest_path)
        log(f"downloaded: {asset.name}", "success")

    def mirror_repository(self, repo: Repository) -> MirrorResult:
        result = MirrorResult()
        repo_path = repo.get_local_path(self.config.base_dir)
        client = self._get_client(repo.platform)
        requests_before = client.request_count

        log(f"mirroring {repo.full_name} ({repo.platform.value})")

        try:
            releases = client.get_releases(repo)
        except RateLimitError as e:
            wait_time = max(0, e.reset_time - int(time.time())) + 1
            log(f"rate limited, waiting {wait_time}s...", "warning")
            time.sleep(wait_time)
            try:
                releases = client.get_releases(repo)
            except (ConnectionError, FileNotFoundError, PermissionError, RuntimeError) as retry_err:
                result.errors.append(f"failed after rate limit wait: {retry_err}")
                log(f"failed after rate limit wait: {retry_err}", "error")
                return result
        except FileNotFoundError:
            result.errors.append(f"repository not found: {repo.full_name}")
            log(f"repository not found: {repo.full_name}", "error")
            return result
        except ConnectionError as e:
            result.errors.append(f"failed to fetch releases: {e}")
            log(f"failed to fetch releases: {e}", "error")
            return result
        except PermissionError as e:
            result.errors.append(f"access denied: {e}")
            log(f"access denied: {e}", "error")
            return result

        log(f"found {len(releases)} releases", "debug")

        latest_release: Release | None = None

        for release in releases:
            if release.draft:
                log(f"skipping draft: {release.tag_name}", "debug")
                continue

            if not release.prerelease and latest_release is None:
                latest_release = release

            release_dir = repo_path / release.tag_name

            if not self.dry_run:
                release_dir.mkdir(parents=True, exist_ok=True)

            checksum_assets = [a for a in release.assets if is_checksum_file(a.name)]
            regular_assets = [a for a in release.assets if not is_checksum_file(a.name)]

            # download checksum files first, parse them for hash lookup
            hash_lookup: dict[str, str] = {}
            for asset in checksum_assets:
                self._download_asset(asset, release_dir, result, client)
                checksum_path = release_dir / asset.name
                if checksum_path.exists():
                    try:
                        content = checksum_path.read_text(encoding="utf-8", errors="ignore")
                        parsed = parse_checksums(content, sidecar_name=asset.name)
                        hash_lookup.update(parsed)
                        if parsed:
                            log(f"parsed {len(parsed)} hashes from {asset.name}", "debug")
                    except OSError as e:
                        log(f"failed to read {asset.name}: {e}", "warning")

            for asset in regular_assets:
                digest_override = None
                if not asset.digest and asset.name in hash_lookup:
                    digest_override = hash_lookup[asset.name]
                    log(f"using hash from sidecar for {asset.name}", "debug")
                self._download_asset(asset, release_dir, result, client, digest_override)

        if latest_release:
            self._update_latest_symlink(repo_path, latest_release.tag_name)

        result.api_requests = client.request_count - requests_before
        return result

    def mirror_all(self) -> MirrorResult:
        total_result = MirrorResult()

        for url in self.config.urls:
            try:
                repo = Repository.from_url(url)
            except ValueError as e:
                total_result.errors.append(str(e))
                log(str(e), "error")
                continue

            result = self.mirror_repository(repo)

            total_result.downloaded.extend(result.downloaded)
            total_result.skipped.extend(result.skipped)
            total_result.errors.extend(result.errors)
            total_result.hash_mismatches.extend(result.hash_mismatches)
            total_result.api_requests += result.api_requests

        return total_result

    def estimate_repository(self, repo: Repository) -> tuple[int, int]:
        """estimate releases and api requests for a repo (uses 1 api request)"""
        client = self._get_client(repo.platform)
        try:
            release_count = client.get_release_count(repo)
            # ceil(releases / 100) requests to fetch all
            api_requests = (release_count + 99) // 100
            return release_count, api_requests
        except (FileNotFoundError, ConnectionError, PermissionError, RuntimeError) as e:
            log(f"failed to estimate {repo.full_name}: {e}", "error")
            return 0, 0

    def estimate_all(self, urls: list[str]) -> None:
        has_github = any(
            "github" in u or u.startswith("gh:") or ("/" in u and not u.startswith("http"))
            for u in urls
        )
        has_gitlab = any("gitlab" in u or u.startswith("gl:") for u in urls)

        if has_github:
            gh_client = self._get_client(Platform.GITHUB)
            if isinstance(gh_client, GitHubClient):
                try:
                    rl = gh_client.get_rate_limit()
                    log(f"github rate limit: {rl.remaining}/{rl.limit} remaining")
                except (ConnectionError, RuntimeError, PermissionError):
                    log("github rate limit: unknown", "warning")

        if has_gitlab:
            gl_client = self._get_client(Platform.GITLAB)
            if isinstance(gl_client, GitLabClient):
                gl_rl = gl_client.get_rate_limit()
                if gl_rl:
                    log(f"gitlab rate limit: {gl_rl.remaining}/{gl_rl.limit} remaining")
                else:
                    log("gitlab rate limit: not enabled or unknown")

        total_releases = 0
        total_requests = 0
        estimate_requests = 0

        log("estimating api requests...")

        for url in urls:
            try:
                repo = Repository.from_url(url)
            except ValueError as e:
                log(str(e), "error")
                continue

            releases, requests = self.estimate_repository(repo)
            estimate_requests += 1  # the estimate itself costs 1 request
            total_releases += releases
            total_requests += requests
            log(f"  {repo.full_name}: {releases} releases -> {requests} requests")

        log(f"total: {total_releases} releases")
        log(f"api requests for estimate: {estimate_requests}")
        log(f"api requests for full mirror: {total_requests}")
