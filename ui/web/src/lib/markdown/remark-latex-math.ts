// meiva:note
// Micromark math tokenizers for Pandoc-bounded single-dollar inline math (`$...$`) and explicit
// LaTeX delimiters (`\(...\)` inline, `\[...\]` display). `remark-math` runs alongside this plugin
// with `singleDollarTextMath: false` so it still owns `$$...$$` and the `mdast-util-math` compiler,
// while every rejected `$`, `\(` or `\[` rolls back during tokenization before emphasis, inline
// code, autolinks or CommonMark character escapes run.
// meiva:link ui/web/src/docs/frontend-components/markdown-safe-content.meiva.okf.md

import type {} from "micromark-extension-math";
import type {
  Code,
  Construct,
  Effects,
  Event,
  Extension as MicromarkExtension,
  State,
  TokenizeContext,
} from "micromark-util-types";
import type {} from "remark-parse";
import type { Processor } from "unified";

import {
  carriesProseOutsideText,
  isInlineBracketDisplayMath,
  isValidSingleDollarMath,
} from "./latex-math-guard";

const CODE_SPACE = 32;
const CODE_DOLLAR = 36;
const CODE_OPEN_PAREN = 40;
const CODE_CLOSE_PAREN = 41;
const CODE_ZERO = 48;
const CODE_NINE = 57;
const CODE_UPPER_A = 65;
const CODE_UPPER_Z = 90;
const CODE_OPEN_BRACKET = 91;
const CODE_BACKSLASH = 92;
const CODE_CLOSE_BRACKET = 93;
const CODE_LOWER_A = 97;
const CODE_LOWER_Z = 122;

function isLineEnding(code: Code): boolean {
  return code !== null && code < -2;
}

function isSpace(code: Code): boolean {
  return code === -2 || code === -1 || code === CODE_SPACE;
}

function isSpaceOrLineEnding(code: Code): boolean {
  return code !== null && (code < 0 || code === CODE_SPACE);
}

function isAsciiDigit(code: Code): boolean {
  return code !== null && code >= CODE_ZERO && code <= CODE_NINE;
}

function isAsciiAlphanumeric(code: Code): boolean {
  if (code === null) return false;
  return (
    (code >= CODE_ZERO && code <= CODE_NINE) ||
    (code >= CODE_UPPER_A && code <= CODE_UPPER_Z) ||
    (code >= CODE_LOWER_A && code <= CODE_LOWER_Z)
  );
}

function appendCode(raw: string, code: number): string {
  if (code === -2) return `${raw}\t`;
  if (code < 0) return raw;
  return raw + String.fromCharCode(code);
}

function isAtLineStart(events: Event[]): boolean {
  const prevType = events.at(-1)?.[1].type;
  return prevType === undefined || prevType === "lineEnding" || prevType === "linePrefix";
}

function consumeSpaces(
  effects: Effects,
  next: State,
  type: "linePrefix" | "whitespace",
  max?: number,
): State {
  let count = 0;
  const step: State = (code) => {
    if (isSpace(code) && (max === undefined || count < max)) {
      if (count === 0) effects.enter(type);
      count += 1;
      effects.consume(code);
      return step;
    }
    if (count > 0) effects.exit(type);
    return next(code);
  };
  return step;
}

const singleDollarText: Construct = {
  name: "mathSingleDollar",
  add: "after",
  previous(this: TokenizeContext, code: Code): boolean {
    const fromEscape = this.events.at(-1)?.[1].type === "characterEscape";
    if (code === CODE_DOLLAR && !fromEscape) return false;
    if (!fromEscape && isAsciiAlphanumeric(code)) return false;
    return true;
  },
  tokenize: tokenizeSingleDollar,
};

function tokenizeSingleDollar(
  this: TokenizeContext,
  effects: Effects,
  ok: State,
  nok: State,
): State {
  let raw = "";
  let prevSpace = false;
  let escaped = false;

  const start: State = (code) => {
    effects.enter("mathText");
    effects.enter("mathTextSequence");
    effects.consume(code);
    effects.exit("mathTextSequence");
    return afterOpen;
  };

  const afterOpen: State = (code) => {
    if (code === null || code === CODE_DOLLAR || isSpaceOrLineEnding(code)) {
      return nok(code);
    }
    effects.enter("mathTextData");
    return inside(code);
  };

  const inside: State = (code) => {
    if (code === null || isLineEnding(code)) {
      return nok(code);
    }
    if (escaped) {
      escaped = false;
      prevSpace = isSpace(code);
      raw = appendCode(raw, code);
      effects.consume(code);
      return inside;
    }
    if (code === CODE_BACKSLASH) {
      escaped = true;
      prevSpace = false;
      raw += "\\";
      effects.consume(code);
      return inside;
    }
    if (code === CODE_DOLLAR) {
      if (prevSpace || !isValidSingleDollarMath(raw)) {
        return nok(code);
      }
      effects.exit("mathTextData");
      effects.enter("mathTextSequence");
      effects.consume(code);
      effects.exit("mathTextSequence");
      return afterClose;
    }
    prevSpace = isSpace(code);
    raw = appendCode(raw, code);
    effects.consume(code);
    return inside;
  };

  const afterClose: State = (code) => {
    if (code === CODE_DOLLAR || isAsciiDigit(code)) {
      return nok(code);
    }
    effects.exit("mathText");
    return ok(code);
  };

  return start;
}

const escapedLatexText: Construct = {
  name: "mathLatexText",
  add: "before",
  tokenize: tokenizeEscapedLatexText,
};

function tokenizeEscapedLatexText(
  this: TokenizeContext,
  effects: Effects,
  ok: State,
  nok: State,
): State {
  const atLineStart = isAtLineStart(this.events);
  let closeMarker = CODE_CLOSE_PAREN;
  let isDisplay = false;
  let raw = "";
  let hasLineEnding = false;
  let inData = false;
  let escapedBackslash = false;

  const closeData = (): void => {
    if (inData) {
      effects.exit("mathTextData");
      inData = false;
    }
  };

  const openData = (): void => {
    if (!inData) {
      effects.enter("mathTextData");
      inData = true;
    }
  };

  const closeCheck: Construct = {
    partial: true,
    tokenize(checkEffects, checkOk, checkNok) {
      const checkSlash: State = (code) => {
        if (code !== CODE_BACKSLASH) return checkNok(code);
        checkEffects.consume(code);
        return checkMarker;
      };
      const checkMarker: State = (code) => {
        if (code !== closeMarker) return checkNok(code);
        checkEffects.consume(code);
        return checkOk;
      };
      return checkSlash;
    },
  };

  const start: State = (code) => {
    effects.enter("mathText");
    effects.enter("mathTextSequence");
    effects.consume(code);
    return openMarker;
  };

  const openMarker: State = (code) => {
    if (code === CODE_OPEN_PAREN) {
      closeMarker = CODE_CLOSE_PAREN;
      isDisplay = false;
    } else if (code === CODE_OPEN_BRACKET) {
      closeMarker = CODE_CLOSE_BRACKET;
      isDisplay = true;
    } else {
      return nok(code);
    }
    effects.consume(code);
    effects.exit("mathTextSequence");
    return between;
  };

  const between: State = (code) => {
    if (code === null) {
      return nok(code);
    }
    if (isLineEnding(code)) {
      escapedBackslash = false;
      hasLineEnding = true;
      raw += "\n";
      closeData();
      effects.enter("lineEnding");
      effects.consume(code);
      effects.exit("lineEnding");
      return between;
    }
    if (escapedBackslash) {
      escapedBackslash = false;
      openData();
      raw = appendCode(raw, code);
      effects.consume(code);
      return between;
    }
    if (code === CODE_BACKSLASH) {
      return effects.check(closeCheck, beforeCloseSequence, consumeBackslash)(code);
    }
    openData();
    raw = appendCode(raw, code);
    effects.consume(code);
    return between;
  };

  const consumeBackslash: State = (code) => {
    escapedBackslash = true;
    openData();
    raw += "\\";
    effects.consume(code);
    return between;
  };

  const beforeCloseSequence: State = (code) => {
    if (raw.trim().length === 0 || carriesProseOutsideText(raw)) {
      return nok(code);
    }
    closeData();
    effects.enter("mathTextSequence");
    effects.consume(code);
    return finishCloseSequence;
  };

  const finishCloseSequence: State = (code) => {
    effects.consume(code);
    effects.exit("mathTextSequence");
    if (!isDisplay) {
      effects.exit("mathText");
      return ok;
    }
    return afterDisplayClose;
  };

  const afterDisplayClose: State = (code) => {
    const atLineEnd = code === null || isLineEnding(code);
    if (!(atLineStart && atLineEnd) && !hasLineEnding && !isInlineBracketDisplayMath(raw)) {
      return nok(code);
    }
    effects.exit("mathText");
    return ok(code);
  };

  return start;
}

const nonLazyContinuation: Construct = {
  partial: true,
  tokenize: tokenizeNonLazyContinuation,
};

function tokenizeNonLazyContinuation(
  this: TokenizeContext,
  effects: Effects,
  ok: State,
  nok: State,
): State {
  const start: State = (code) => {
    if (code === null) return ok(code);
    effects.enter("lineEnding");
    effects.consume(code);
    effects.exit("lineEnding");
    return lineStart;
  };
  const lineStart: State = (code) => {
    return this.parser.lazy[this.now().line] ? nok(code) : ok(code);
  };
  return start;
}

const bracketMathFlow: Construct = {
  name: "mathFlowBracket",
  concrete: true,
  tokenize: tokenizeBracketMathFlow,
};

function maxClosingFenceIndent(context: TokenizeContext): number | undefined {
  const disabled = context.parser.constructs.disable;
  return disabled.null?.includes("codeIndented") ? undefined : 3;
}

function tokenizeBracketMathFlow(
  this: TokenizeContext,
  effects: Effects,
  ok: State,
  nok: State,
): State {
  const tail = this.events.at(-1);
  const initialSize =
    tail?.[1].type === "linePrefix" ? tail[2].sliceSerialize(tail[1], true).length : 0;
  const maxFenceIndent = maxClosingFenceIndent(this);

  const closingFence: Construct = {
    partial: true,
    tokenize(fenceEffects, fenceOk, fenceNok) {
      const beforeClose: State = (code) => {
        if (code !== CODE_BACKSLASH) return fenceNok(code);
        fenceEffects.enter("mathFlowFence");
        fenceEffects.enter("mathFlowFenceSequence");
        fenceEffects.consume(code);
        return closeBracket;
      };
      const closeBracket: State = (code) => {
        if (code !== CODE_CLOSE_BRACKET) return fenceNok(code);
        fenceEffects.consume(code);
        fenceEffects.exit("mathFlowFenceSequence");
        return consumeSpaces(fenceEffects, afterCloseFence, "whitespace");
      };
      const afterCloseFence: State = (code) => {
        if (code === null || isLineEnding(code)) {
          fenceEffects.exit("mathFlowFence");
          return fenceOk(code);
        }
        return fenceNok(code);
      };
      return consumeSpaces(fenceEffects, beforeClose, "linePrefix", maxFenceIndent);
    },
  };

  const start: State = (code) => {
    effects.enter("mathFlow");
    effects.enter("mathFlowFence");
    effects.enter("mathFlowFenceSequence");
    effects.consume(code);
    return openBracket;
  };

  const openBracket: State = (code) => {
    if (code !== CODE_OPEN_BRACKET) return nok(code);
    effects.consume(code);
    effects.exit("mathFlowFenceSequence");
    return consumeSpaces(effects, afterOpenFence, "whitespace");
  };

  const afterOpenFence: State = (code) => {
    if (!isLineEnding(code)) return nok(code);
    effects.exit("mathFlowFence");
    if (this.interrupt) return ok(code);
    return effects.attempt(nonLazyContinuation, beforeLine, afterBlock)(code);
  };

  const beforeLine: State = (code) => {
    return effects.attempt(closingFence, afterBlock, contentStart)(code);
  };

  const contentStart: State = (code) => {
    return initialSize > 0
      ? consumeSpaces(effects, beforeContentChunk, "linePrefix", initialSize)(code)
      : beforeContentChunk(code);
  };

  const beforeContentChunk: State = (code) => {
    if (code === null) return afterBlock(code);
    if (isLineEnding(code)) {
      return effects.attempt(nonLazyContinuation, beforeLine, afterBlock)(code);
    }
    effects.enter("mathFlowValue");
    return contentChunk(code);
  };

  const contentChunk: State = (code) => {
    if (code === null || isLineEnding(code)) {
      effects.exit("mathFlowValue");
      return beforeContentChunk(code);
    }
    effects.consume(code);
    return contentChunk;
  };

  const afterBlock: State = (code) => {
    effects.exit("mathFlow");
    return ok(code);
  };

  return start;
}

const latexMathMicromark: MicromarkExtension = {
  flow: {
    [CODE_BACKSLASH]: bracketMathFlow,
  },
  text: {
    [CODE_DOLLAR]: singleDollarText,
    [CODE_BACKSLASH]: escapedLatexText,
  },
};

export default function remarkLatexMath(this: Processor): undefined {
  const data = this.data();
  const micromarkExtensions = (data.micromarkExtensions ??= []);
  micromarkExtensions.push(latexMathMicromark);
}
