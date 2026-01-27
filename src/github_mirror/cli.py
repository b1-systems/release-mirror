import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .log import log
from .mirror import ReleaseMirror
from .models import Config, Repository


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(message)s"
    logging.basicConfig(level=level, format=fmt)


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="release-mirror",
        description="mirror github/gitlab releases locally",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  release-mirror -c config.toml
  release-mirror -c config.toml --dry-run
  release-mirror --repo trufflesecurity/trufflehog --base-dir ./mirror
  release-mirror --repo https://gitlab.com/gitlab-org/gitlab-runner --base-dir ./mirror
  release-mirror --repo gl:gitlab-org/gitlab-runner --base-dir ./mirror --gitlab-token TOKEN
""",
    )

    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        help="path to config file (toml or yaml)",
    )

    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="show what would be done without downloading",
    )

    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="verbose output",
    )

    parser.add_argument(
        "--repo",
        type=str,
        help="single repo to mirror (owner/repo, github/gitlab url, or gh:/gl: prefix)",
    )

    parser.add_argument(
        "--base-dir",
        type=Path,
        help="base directory for downloads (overrides config)",
    )

    parser.add_argument(
        "--proxy",
        type=str,
        help="proxy server (host:port, overrides config)",
    )

    parser.add_argument(
        "--token",
        type=str,
        help="github token (overrides config)",
    )

    parser.add_argument(
        "--gitlab-token",
        type=str,
        help="gitlab private token (overrides config)",
    )

    parser.add_argument(
        "--estimate",
        action="store_true",
        help="estimate api requests needed without downloading",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = create_parser()
    args = parser.parse_args(argv)

    setup_logging(args.verbose)

    if not args.config and not args.repo:
        parser.error("either --config or --repo is required")

    config: Config

    if args.config:
        if not args.config.exists():
            log(f"config file not found: {args.config}", "error")
            return 1

        try:
            config = Config.from_file(args.config)
        except (KeyError, ValueError) as e:
            log(f"invalid config: {e}", "error")
            return 1
    else:
        if not args.base_dir:
            parser.error("--base-dir is required when using --repo")

        config = Config(
            base_dir=args.base_dir,
            proxy=None,
            urls=[],
            github_token=None,
            gitlab_token=None,
        )

    if args.base_dir:
        config.base_dir = args.base_dir

    if args.proxy:
        config.proxy = args.proxy

    if args.token:
        config.github_token = args.token

    if args.gitlab_token:
        config.gitlab_token = args.gitlab_token

    mirror = ReleaseMirror(
        config=config,
        dry_run=args.dry_run,
    )

    if args.estimate:
        urls = [args.repo] if args.repo else config.urls
        if not urls:
            log("no urls to estimate", "warning")
            return 0
        mirror.estimate_all(urls)
        return 0

    if args.dry_run:
        log("dry-run mode enabled")

    if args.repo:
        try:
            repo = Repository.from_url(args.repo)
        except ValueError as e:
            log(str(e), "error")
            return 1

        result = mirror.mirror_repository(repo)
    else:
        if not config.urls:
            log("no urls configured", "warning")
            return 0

        result = mirror.mirror_all()

    log(f"api requests: {result.api_requests}")
    log(f"downloaded: {len(result.downloaded)}")
    log(f"skipped: {len(result.skipped)}")

    if result.errors:
        log(f"errors: {len(result.errors)}", "error")

    if result.hash_mismatches:
        log(f"hash mismatches: {len(result.hash_mismatches)}", "error")
        for err in result.hash_mismatches:
            log(f"  {err.path}: expected {err.expected[:16]}...", "error")

    if result.success:
        log("done", "success")
        return 0
    else:
        log("completed with errors", "error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
