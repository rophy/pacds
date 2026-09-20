import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { ensureRepo, type EnsureRepoOpts } from "./repo-cloner.js";

const REPO_DIR = "/tmp/test-repos/test-service";

const baseOpts: EnsureRepoOpts = {
  repoDir: REPO_DIR,
  gitUrl: "https://git.example.com",
  gitToken: "test-token",
  repoPath: "teams/commerce/checkout",
  service: "test-service",
};

vi.mock("node:child_process", () => ({
  execFile: vi.fn((_cmd: string, _args: string[], _opts: unknown, cb: Function) => {
    cb(null, "", "");
  }),
}));

describe("ensureRepo", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    rmSync("/tmp/test-repos", { recursive: true, force: true });
  });

  it("skips clone when repo directory already has content", async () => {
    mkdirSync(REPO_DIR, { recursive: true });
    writeFileSync(`${REPO_DIR}/main.go`, "package main");

    const { execFile } = await import("node:child_process");
    const result = await ensureRepo(baseOpts);

    expect(result).toBeNull();
    expect(execFile).not.toHaveBeenCalled();
  });

  it("clones repo when directory does not exist", async () => {
    const { execFile } = await import("node:child_process");
    const result = await ensureRepo(baseOpts);

    expect(result).toBeNull();
    expect(execFile).toHaveBeenCalledWith(
      "git",
      ["clone", "--depth", "1", "--single-branch", expect.stringContaining("teams/commerce/checkout"), REPO_DIR],
      expect.objectContaining({ timeout: 120_000 }),
      expect.any(Function),
    );
  });

  it("embeds token in clone URL", async () => {
    const { execFile } = await import("node:child_process");
    await ensureRepo(baseOpts);

    const cloneUrl = (execFile as ReturnType<typeof vi.fn>).mock.calls[0][1][4];
    expect(cloneUrl).toContain("oauth2");
    expect(cloneUrl).toContain("test-token");
  });

  it("clones repo when directory exists but is empty", async () => {
    mkdirSync(REPO_DIR, { recursive: true });

    const { execFile } = await import("node:child_process");
    const result = await ensureRepo(baseOpts);

    expect(result).toBeNull();
    expect(execFile).toHaveBeenCalled();
  });

  it("returns error when gitUrl is not configured", async () => {
    const result = await ensureRepo({ ...baseOpts, gitUrl: "" });

    expect(result).toContain("no GIT_URL configured");
    expect(result).toContain("test-service");
  });

  it("returns auth error on git authentication failure", async () => {
    const { execFile } = await import("node:child_process");
    (execFile as ReturnType<typeof vi.fn>).mockImplementationOnce(
      (_cmd: string, _args: string[], _opts: unknown, cb: Function) => {
        cb(new Error("git error"), "", "Authentication failed for 'https://git.example.com'");
      },
    );

    const result = await ensureRepo(baseOpts);

    expect(result).toContain("authentication error");
    expect(result).toContain("GIT_TOKEN");
  });

  it("returns not-found error when repo does not exist", async () => {
    const { execFile } = await import("node:child_process");
    (execFile as ReturnType<typeof vi.fn>).mockImplementationOnce(
      (_cmd: string, _args: string[], _opts: unknown, cb: Function) => {
        cb(new Error("git error"), "", "repository 'https://git.example.com/foo' not found");
      },
    );

    const result = await ensureRepo(baseOpts);

    expect(result).toContain("repository not found");
    expect(result).toContain("test-service");
  });

  it("returns generic error on unexpected clone failure", async () => {
    const { execFile } = await import("node:child_process");
    (execFile as ReturnType<typeof vi.fn>).mockImplementationOnce(
      (_cmd: string, _args: string[], _opts: unknown, cb: Function) => {
        cb(new Error("git error"), "", "fatal: unable to access");
      },
    );

    const result = await ensureRepo(baseOpts);

    expect(result).toContain("Git clone failed");
    expect(result).toContain("unable to access");
  });
});
