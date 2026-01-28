from pathlib import Path

import pytest

from release_mirror.models import Platform, Repository


class TestRepository:
    def test_from_github_url(self) -> None:
        repo = Repository.from_url("https://github.com/owner/repo")
        assert repo.owner == "owner"
        assert repo.repo == "repo"
        assert repo.platform == Platform.GITHUB
        assert repo.base_url is None

    def test_from_github_url_with_trailing_slash(self) -> None:
        repo = Repository.from_url("https://github.com/owner/repo/")
        assert repo.owner == "owner"
        assert repo.repo == "repo"

    def test_from_github_url_with_git_suffix(self) -> None:
        repo = Repository.from_url("https://github.com/owner/repo.git")
        assert repo.owner == "owner"
        assert repo.repo == "repo"

    def test_from_github_shorthand(self) -> None:
        repo = Repository.from_url("gh:owner/repo")
        assert repo.owner == "owner"
        assert repo.repo == "repo"
        assert repo.platform == Platform.GITHUB

    def test_from_bare_path_defaults_to_github(self) -> None:
        repo = Repository.from_url("owner/repo")
        assert repo.owner == "owner"
        assert repo.repo == "repo"
        assert repo.platform == Platform.GITHUB

    def test_from_gitlab_url(self) -> None:
        repo = Repository.from_url("https://gitlab.com/owner/repo")
        assert repo.owner == "owner"
        assert repo.repo == "repo"
        assert repo.platform == Platform.GITLAB
        assert repo.base_url == "https://gitlab.com"

    def test_from_gitlab_shorthand(self) -> None:
        repo = Repository.from_url("gl:owner/repo")
        assert repo.owner == "owner"
        assert repo.repo == "repo"
        assert repo.platform == Platform.GITLAB
        assert repo.base_url is None

    def test_from_self_hosted_gitlab(self) -> None:
        repo = Repository.from_url("https://gitlab.example.com/owner/repo")
        assert repo.owner == "owner"
        assert repo.repo == "repo"
        assert repo.platform == Platform.GITLAB
        assert repo.base_url == "https://gitlab.example.com"

    def test_invalid_url_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid repository url"):
            Repository.from_url("not-a-repo")

    def test_unknown_host_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown host"):
            Repository.from_url("https://bitbucket.org/owner/repo")
