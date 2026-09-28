import asyncio
import subprocess

import pytest

from pacds.config import GitConfig, GitCredential
from pacds.errors import PacdsError
from pacds.workspace.git import GitFetcher


def fetcher(tmp_path, **config) -> GitFetcher:
    return GitFetcher(GitConfig(cache_dir=tmp_path / "cache", **config), allowed_protocols=("file",))


async def test_checkout_branch(tmp_path, origin):
    url, sha = origin
    checkout = await fetcher(tmp_path).checkout(url, "main")
    assert checkout.sha == sha
    assert (checkout.path / "app.py").read_text().startswith("def checkout")


async def test_checkout_tag_and_full_sha(tmp_path, origin):
    url, sha = origin
    git = fetcher(tmp_path)
    assert (await git.checkout(url, "v1")).sha == sha
    assert (await git.checkout(url, sha)).sha == sha


async def test_unknown_ref_is_422(tmp_path, origin):
    url, _ = origin
    with pytest.raises(PacdsError) as error:
        await fetcher(tmp_path).checkout(url, "no-such-branch")
    assert (error.value.status, error.value.code) == (422, "unknown_ref")


async def test_unreachable_repo_is_502(tmp_path):
    with pytest.raises(PacdsError) as error:
        await fetcher(tmp_path).checkout((tmp_path / "missing.git").as_uri(), "main")
    assert error.value.status == 502


async def test_cache_is_reused(tmp_path, origin, monkeypatch):
    url, _ = origin
    git = fetcher(tmp_path)
    first = await git.checkout(url, "main")

    async def fail(*args, **kwargs):
        raise AssertionError("fetched again")

    monkeypatch.setattr(git, "_fetch", fail)
    assert (await git.checkout(url, "main")).path == first.path


async def test_concurrent_checkouts_fetch_once(tmp_path, origin):
    url, sha = origin
    git = fetcher(tmp_path)
    results = await asyncio.gather(*(git.checkout(url, sha) for _ in range(4)))
    assert len({result.path for result in results}) == 1


async def test_repo_too_large(tmp_path, origin):
    url, _ = origin
    work = tmp_path / "work"
    (work / "big.bin").write_bytes(b"x" * (2 * 1024 * 1024))
    subprocess.run(["git", "add", "."], cwd=work, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "big"], cwd=work, check=True)
    subprocess.run(["git", "push", "-q", str(tmp_path / "origin.git"), "main"], cwd=work, check=True)
    with pytest.raises(PacdsError) as error:
        await fetcher(tmp_path, max_repo_size_mb=1).checkout(url, "main")
    assert (error.value.status, error.value.code) == (422, "repo_too_large")
    assert not any((tmp_path / "cache").rglob("big.bin"))


async def test_credentials_go_through_askpass_env_not_argv(tmp_path, monkeypatch):
    captured = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"a" * 40 + b"\trefs/heads/main\n", b""

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    config = GitConfig(
        cache_dir=tmp_path / "cache",
        credentials=[GitCredential(host="git.example.com", token_env="GIT_TOKEN_EXAMPLE")],
    )
    git = GitFetcher(config, env={"PATH": "/usr/bin", "GIT_TOKEN_EXAMPLE": "s3cret"})
    await git._resolve("https://git.example.com/shop/checkout.git", "main")
    assert "s3cret" not in " ".join(captured["args"])
    assert captured["env"]["PACDS_GIT_PASSWORD"] == "s3cret"
    assert captured["env"]["PACDS_GIT_USERNAME"] == "x-access-token"
    assert captured["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert captured["env"]["GIT_ASKPASS"].endswith("askpass.sh")


async def test_missing_credential_env_is_500(tmp_path):
    config = GitConfig(
        cache_dir=tmp_path / "cache",
        credentials=[GitCredential(host="git.example.com", token_env="GIT_TOKEN_EXAMPLE")],
    )
    with pytest.raises(PacdsError) as error:
        await GitFetcher(config, env={"PATH": "/usr/bin"}).checkout("https://git.example.com/shop/x.git", "main")
    assert error.value.status == 500


async def test_checkout_includes_limited_history_before_the_commit(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e", "PATH": "/usr/bin:/bin"}
    run = lambda *args, cwd=work: subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()  # noqa: E731
    run("init", "-q", "-b", "main")
    for n in range(4):
        (work / "f.txt").write_text(str(n))
        run("add", ".")
        run("commit", "-q", "-m", f"c{n}")
    deployed = run("rev-parse", "HEAD~1")
    bare = tmp_path / "origin.git"
    run("clone", "-q", "--bare", str(work), str(bare), cwd=tmp_path)
    run("config", "uploadpack.allowAnySHA1InWant", "true", cwd=bare)
    git = GitFetcher(GitConfig(cache_dir=tmp_path / "cache", history_depth=1), allowed_protocols=("file",))
    checkout = await git.checkout(bare.as_uri(), deployed)
    assert run("log", "--format=%s", cwd=checkout.path).splitlines() == ["c2", "c1"]  # the deployed commit and one before, never c3
