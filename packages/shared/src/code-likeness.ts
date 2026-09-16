export interface CodeLikenessResult {
  passed: boolean;
  flags: string[];
}

const SYNTAX_CHARS = new Set(["{", "}", ";", "(", ")", "=", ">", "<", "/", "[", "]"]);
const SYNTAX_DENSITY_THRESHOLD = 0.12;

const CODE_KEYWORDS = new Set([
  "function", "const", "let", "var", "return", "import", "export",
  "class", "if", "else", "for", "while", "async", "await", "throw",
  "new", "try", "catch", "switch", "case", "break", "continue",
  "def", "self", "elif", "except", "finally", "yield", "lambda",
  "public", "private", "protected", "static", "void", "int", "string",
]);
const KEYWORD_DENSITY_THRESHOLD = 0.08;

const INDENTED_LINE = /^[ \t]{2,}\S/;
const LINE_STRUCTURE_THRESHOLD = 0.6;
const MIN_LINES_FOR_STRUCTURE = 3;

function checkSyntaxDensity(text: string): boolean {
  if (text.length === 0) return false;
  let count = 0;
  for (const ch of text) {
    if (SYNTAX_CHARS.has(ch)) count++;
  }
  return count / text.length > SYNTAX_DENSITY_THRESHOLD;
}

function checkKeywordClustering(text: string): boolean {
  const words = text.toLowerCase().split(/\s+/);
  if (words.length === 0) return false;
  let count = 0;
  for (const word of words) {
    const cleaned = word.replace(/[^a-z]/g, "");
    if (CODE_KEYWORDS.has(cleaned)) count++;
  }
  return count / words.length > KEYWORD_DENSITY_THRESHOLD;
}

function checkLineStructure(text: string): boolean {
  const lines = text.split("\n").filter((l) => l.trim().length > 0);
  if (lines.length < MIN_LINES_FOR_STRUCTURE) return false;
  let indented = 0;
  for (const line of lines) {
    if (INDENTED_LINE.test(line)) indented++;
  }
  return indented / lines.length > LINE_STRUCTURE_THRESHOLD;
}

export function checkCodeLikeness(text: string): CodeLikenessResult {
  if (text.length === 0) return { passed: true, flags: [] };

  const flags: string[] = [];
  if (checkSyntaxDensity(text)) flags.push("syntax_density");
  if (checkKeywordClustering(text)) flags.push("keyword_clustering");
  if (checkLineStructure(text)) flags.push("line_structure");

  return { passed: flags.length === 0, flags };
}
