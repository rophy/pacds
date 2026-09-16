import { isAbsolute, relative } from "node:path";

export function isInsideRepo(repoDir: string, fullPath: string): boolean {
  const rel = relative(repoDir, fullPath);
  return !rel.startsWith("..") && !isAbsolute(rel);
}
