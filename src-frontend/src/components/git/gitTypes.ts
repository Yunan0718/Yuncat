export interface GitFile {
  path: string;
  status: string;
  staged: string;
}
export interface GitStatus {
  ok: boolean;
  branch: string;
  ahead: number;
  behind: number;
  files: GitFile[];
  error?: string;
}
export interface GitCommit {
  hash: string;
  message: string;
  time: string;
  author: string;
  parents?: string[];
}
export interface GitBranch {
  name: string;
  current: boolean;
  hash?: string;
}

export const STATUS_KEY_MAP: Record<string, string> = {
  M: "modified",
  A: "added",
  D: "deleted",
  R: "renamed",
  U: "unmerged",
  "?": "untracked",
  "!": "ignored",
};
