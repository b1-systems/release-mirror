import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from urllib.parse import urlparse


class Platform(Enum):
    GITHUB = "github"
    GITLAB = "gitlab"


@dataclass
class Asset:
    name: str
    download_url: str
    size: int
    digest: str | None
    updated_at: datetime

    @classmethod
    def from_github_api(cls, data: dict) -> "Asset":
        # github provides sha256:xxxx digest for some assets (e.g. container images)
        # most release assets have no digest
        digest = data.get("digest")
        if digest and digest.startswith("sha256:"):
            digest = digest[7:]

        return cls(
            name=data["name"],
            download_url=data["browser_download_url"],
            size=data["size"],
            digest=digest,
            updated_at=datetime.fromisoformat(
                data["updated_at"].replace("Z", "+00:00")
            ),
        )

    @classmethod
    def from_gitlab_api(cls, data: dict, release_date: datetime) -> "Asset":
        # gitlab release links don't provide size or digest
        return cls(
            name=data["name"],
            download_url=data.get("direct_asset_url") or data["url"],
            size=0,
            digest=None,
            updated_at=release_date,
        )


@dataclass
class Release:
    tag_name: str
    name: str
    draft: bool
    prerelease: bool
    published_at: datetime
    assets: list[Asset] = field(default_factory=list)

    @classmethod
    def from_github_api(cls, data: dict) -> "Release":
        return cls(
            tag_name=data["tag_name"],
            name=data.get("name") or data["tag_name"],
            draft=data["draft"],
            prerelease=data["prerelease"],
            published_at=datetime.fromisoformat(
                data["published_at"].replace("Z", "+00:00")
            ),
            assets=[Asset.from_github_api(a) for a in data.get("assets", [])],
        )

    @classmethod
    def from_gitlab_api(cls, data: dict) -> "Release":
        published_str = data.get("released_at") or data.get("created_at")
        if not published_str:
            raise ValueError("release missing released_at and created_at")
        published_at = datetime.fromisoformat(published_str.replace("Z", "+00:00"))

        assets: list[Asset] = []
        if "assets" in data and "links" in data["assets"]:
            for link in data["assets"]["links"]:
                assets.append(Asset.from_gitlab_api(link, published_at))

        return cls(
            tag_name=data["tag_name"],
            name=data.get("name") or data["tag_name"],
            draft=False,  # gitlab has no draft concept for releases
            prerelease=data.get("upcoming_release", False),
            published_at=published_at,
            assets=assets,
        )


@dataclass
class Repository:
    owner: str
    repo: str
    platform: Platform = Platform.GITHUB
    base_url: str | None = None  # for self-hosted instances

    @classmethod
    def from_url(cls, url: str) -> "Repository":
        url = url.rstrip("/")

        # shorthand: gh:owner/repo or gl:owner/repo
        if url.startswith("gh:"):
            return cls._parse_path(url[3:], Platform.GITHUB, None)

        if url.startswith("gl:"):
            return cls._parse_path(url[3:], Platform.GITLAB, None)

        if url.startswith("http://") or url.startswith("https://"):
            parsed = urlparse(url)
            host = parsed.netloc.lower()

            if host == "github.com" or host == "www.github.com":
                return cls._parse_path(parsed.path.lstrip("/"), Platform.GITHUB, None)

            if "gitlab" in host:
                base = f"{parsed.scheme}://{parsed.netloc}"
                return cls._parse_path(parsed.path.lstrip("/"), Platform.GITLAB, base)

            raise ValueError(f"unknown host: {host}")

        # bare owner/repo - assume github for backward compat
        if "/" in url:
            return cls._parse_path(url, Platform.GITHUB, None)

        raise ValueError(f"invalid repository url: {url}")

    @classmethod
    def _parse_path(
        cls, path: str, platform: Platform, base_url: str | None
    ) -> "Repository":
        path = re.sub(r"\.git$", "", path)  # strip .git suffix
        parts = path.split("/")
        if len(parts) < 2:
            raise ValueError(f"invalid repository path: {path}")
        return cls(
            owner=parts[0],
            repo=parts[1],
            platform=platform,
            base_url=base_url,
        )

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    def get_releases_url(self) -> str:
        if self.platform == Platform.GITHUB:
            return f"https://api.github.com/repos/{self.owner}/{self.repo}/releases"
        elif self.platform == Platform.GITLAB:
            base = self.base_url or "https://gitlab.com"
            project_path = f"{self.owner}%2F{self.repo}"
            return f"{base}/api/v4/projects/{project_path}/releases"
        raise ValueError(f"unsupported platform: {self.platform}")

    def get_local_path(self, base_dir: Path) -> Path:
        if self.platform == Platform.GITHUB:
            return base_dir / "GitHubReleases" / self.owner / self.repo
        elif self.platform == Platform.GITLAB:
            return base_dir / "GitLabReleases" / self.owner / self.repo
        raise ValueError(f"unsupported platform: {self.platform}")


@dataclass
class Config:
    base_dir: Path
    proxy: str | None
    urls: list[str]
    github_token: str | None = None
    gitlab_token: str | None = None

    @classmethod
    def from_file(cls, path: Path) -> "Config":
        suffix = path.suffix.lower()

        if suffix == ".toml":
            import tomllib

            with open(path, "rb") as f:
                data = tomllib.load(f)
        elif suffix in (".yaml", ".yml"):
            import yaml

            with open(path) as f:
                data = yaml.safe_load(f)
        else:
            raise ValueError(f"unsupported config format: {suffix}")

        return cls(
            base_dir=Path(data["base_dir"]).expanduser(),
            proxy=data.get("proxy"),
            urls=data.get("urls", []),
            github_token=data.get("github_token"),
            gitlab_token=data.get("gitlab_token"),
        )
