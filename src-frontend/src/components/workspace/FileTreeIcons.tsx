import {
  File,
  FileCode,
  FileJson,
  FileType,
  FileImage,
  FileText,
  FileSpreadsheet,
  FileTerminal,
  FileArchive,
  FileMusic,
  FileVideo,
  type LucideIcon,
} from "lucide-react";

interface FileIconMeta {
  icon: LucideIcon;
  color: string;
}

const EXT_MAP: Record<string, FileIconMeta> = {
  ts: { icon: FileCode, color: "text-blue-500" },
  tsx: { icon: FileCode, color: "text-sky-500" },
  js: { icon: FileCode, color: "text-yellow-500" },
  jsx: { icon: FileCode, color: "text-amber-500" },
  py: { icon: FileCode, color: "text-emerald-500" },
  rs: { icon: FileCode, color: "text-orange-500" },
  go: { icon: FileCode, color: "text-cyan-500" },
  java: { icon: FileCode, color: "text-red-400" },
  cpp: { icon: FileCode, color: "text-indigo-400" },
  c: { icon: FileCode, color: "text-indigo-400" },
  h: { icon: FileCode, color: "text-purple-400" },
  json: { icon: FileJson, color: "text-yellow-400" },
  yaml: { icon: FileType, color: "text-red-400" },
  yml: { icon: FileType, color: "text-red-400" },
  toml: { icon: FileType, color: "text-gray-400" },
  md: { icon: FileText, color: "text-blue-400" },
  txt: { icon: FileText, color: "text-muted-foreground" },
  csv: { icon: FileSpreadsheet, color: "text-green-500" },
  sh: { icon: FileTerminal, color: "text-green-400" },
  bash: { icon: FileTerminal, color: "text-green-400" },
  zsh: { icon: FileTerminal, color: "text-green-400" },
  // 与其他扩展名保持同一档位（500）：600 档在暗色主题下偏暗、对比不足
  zip: { icon: FileArchive, color: "text-yellow-500" },
  tar: { icon: FileArchive, color: "text-yellow-500" },
  gz: { icon: FileArchive, color: "text-yellow-500" },
  mp3: { icon: FileMusic, color: "text-pink-400" },
  mp4: { icon: FileVideo, color: "text-pink-400" },
  png: { icon: FileImage, color: "text-purple-400" },
  jpg: { icon: FileImage, color: "text-purple-400" },
  jpeg: { icon: FileImage, color: "text-purple-400" },
  gif: { icon: FileImage, color: "text-purple-400" },
  svg: { icon: FileImage, color: "text-purple-400" },
  webp: { icon: FileImage, color: "text-purple-400" },
  pdf: { icon: FileText, color: "text-red-500" },
};

export function getFileIconMeta(name: string): FileIconMeta {
  const ext = name.split(".").pop()?.toLowerCase() || "";
  return EXT_MAP[ext] || { icon: File, color: "text-muted-foreground" };
}
