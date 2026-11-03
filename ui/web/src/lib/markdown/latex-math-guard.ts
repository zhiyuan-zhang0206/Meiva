// meiva:note
// Pure lexical predicates that separate inline LaTeX math from prose and currency before micromark
// commits a token: CJK or typography punctuation outside TeX text-mode groups, unbraced Markdown
// `**` emphasis runs, and currency-leading phrases (`$30 for ...`, `($30) and ...`) reject a
// candidate `$...$` span so surrounding emphasis, code and links still parse normally.
// meiva:link ui/web/src/docs/frontend-components/markdown-safe-content.meiva.okf.md

const FENCED_MATH = /(?:^|\n)[ \t]{0,3}(?:`{3,}|~{3,})[ \t]*math\b/;

const TEX_TEXT_COMMAND =
  /^\\(?:text(?:bf|it|rm|sf|tt|normal)?|mbox|hbox|operatorname\*?|tag\*?)\s*\{/;

const PROSE_OUTSIDE_TEXT =
  /\p{Script=Han}|\p{Script=Hiragana}|\p{Script=Katakana}|\p{Script=Hangul}|[\u2013-\u201F\u2026\u3000-\u303F\uFF00-\uFFEF"]/u;

const CURRENCY_LEADING_PROSE =
  /^\d+(?:[.,]\d+)*[kKmMbB]?(?:\/[A-Za-z]{2,}(?=[\s,;:.!?)])|[);:!?]|\s+[A-Za-z]{2,}(?![(_^])|,\s+[A-Za-z]{2,}(?![(_^]))/;

export function hasMathSyntax(content: string): boolean {
  return (
    content.includes("$") ||
    content.includes("\\(") ||
    content.includes("\\[") ||
    FENCED_MATH.test(content)
  );
}

export function stripTexTextGroups(value: string): string {
  let result = "";
  let index = 0;
  while (index < value.length) {
    if (value[index] === "\\") {
      const match = TEX_TEXT_COMMAND.exec(value.slice(index));
      if (match !== null) {
        let cursor = index + match[0].length;
        let depth = 1;
        while (cursor < value.length && depth > 0) {
          if (value[cursor] === "\\") {
            cursor += 2;
            continue;
          }
          if (value[cursor] === "{") depth += 1;
          else if (value[cursor] === "}") depth -= 1;
          cursor += 1;
        }
        if (depth === 0) {
          result += "\\text{}";
          index = cursor;
          continue;
        }
      }
      if (index + 1 < value.length) {
        result += value.slice(index, index + 2);
        index += 2;
        continue;
      }
    }
    result += value[index];
    index += 1;
  }
  return result;
}

function hasUnbracedDoubleStar(value: string): boolean {
  let depth = 0;
  for (let index = 0; index < value.length; index += 1) {
    const ch = value[index];
    if (ch === "\\") {
      index += 1;
      continue;
    }
    if (ch === "{") {
      depth += 1;
    } else if (ch === "}" && depth > 0) {
      depth -= 1;
    } else if (depth === 0 && ch === "*" && value[index + 1] === "*") {
      return true;
    }
  }
  return false;
}

export function carriesProseOutsideText(raw: string): boolean {
  if (raw.includes("`")) return true;
  return PROSE_OUTSIDE_TEXT.test(stripTexTextGroups(raw));
}

export function isValidSingleDollarMath(raw: string): boolean {
  if (raw.length === 0 || raw.includes("`")) return false;
  const stripped = stripTexTextGroups(raw);
  if (PROSE_OUTSIDE_TEXT.test(stripped)) return false;
  if (hasUnbracedDoubleStar(stripped)) return false;
  if (CURRENCY_LEADING_PROSE.test(stripped)) return false;
  return true;
}

export function isInlineBracketDisplayMath(raw: string): boolean {
  if (raw.trim().length === 0 || carriesProseOutsideText(raw)) return false;
  return /^\s|\s$/.test(raw) || /[\\^={}+<>]/.test(raw);
}
