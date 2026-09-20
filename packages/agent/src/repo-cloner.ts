import { execFile } from "node:child_process";
import { readdirSync } from "node:fs";

// TODO: refactor to support shared repo cache, sparse checkouts, and
// pre-warmed volumes for better scalability with large monorepos.

export interface EnsureRepoOpts {
  repoDir: string;
  gitUrl: string;
  gitToken: string;
  repoPath: string;
  service: string;
}

export async function ensureRepo(opts: EnsureRepoOpts): Promise<string | null> {
  try {
    const entries = readdirSync(opts.repoDir);
    if (entries.length > 0) return null;
  } catch {
    // directory doesn't exist — proceed to clone
  }

  if (!opts.gitUrl) {
    return `Source code not available: no GIT_URL configured and repository for "${opts.service}" has not been cloned.`;
  }

  const cloneUrl = buildCloneUrl(opts.gitUrl, opts.gitToken, opts.repoPath);

  try {
    await gitClone(cloneUrl, opts.repoDir);
    return null;
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    if (message.includes("Authentication failed") || message.includes("could not read Username")) {
      return `Git clone failed: authentication error for "${opts.service}". Check GIT_TOKEN.`;
    }
    if (message.includes("not found") || message.includes("does not exist")) {
      return `Git clone failed: repository not found for "${opts.service}" at ${opts.gitUrl}/${opts.repoPath}.`;
    }
    return `Git clone failed for "${opts.service}": ${message}`;
  }
}

function buildCloneUrl(gitUrl: string, gitToken: string, repoPath: string): string {
  if (!gitToken) return `${gitUrl}/${repoPath}`;

  try {
    const url = new URL(`${gitUrl}/${repoPath}`);
    url.username = "oauth2";
    url.password = gitToken;
    return url.toString();
  } catch {
    return `${gitUrl}/${repoPath}`;
  }
}

function gitClone(url: string, dest: string): Promise<void> {
  return new Promise((resolve, reject) => {
    execFile(
      "git",
      ["clone", "--depth", "1", "--single-branch", url, dest],
      { timeout: 120_000 },
      (err, _stdout, stderr) => {
        if (err) {
          reject(new Error(stderr.trim() || err.message));
        } else {
          resolve();
        }
      },
    );
  });
}
