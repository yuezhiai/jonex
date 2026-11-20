"""推理规则 DSL v1 解析器（纯函数，手写递归下降）。

文法见 `contracts/reasoning_rules/grammar.md`：
    rule := "IF" atom ("AND" atom)* "THEN" atom
    atom := "?"var REL "?"var | "?"var "是" CLASS
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .schema_context import SchemaContext

MAX_RULE_TEXT_CHARS = 8192
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_KEYWORDS = {"IF", "AND", "THEN", "是"}


@dataclass
class Token:
    kind: str          # VAR / NAME / KW / SYMBOL / EOF
    value: str
    line: int
    column: int


def _issue(code: str, line: int, column: int, params: Optional[dict] = None) -> dict:
    out = {
        "code": code,
        "severity": "Violation",
        "target": {"kind": "rule", "id": "", "field": f"line:{line}:col:{column}"},
    }
    if params:
        out["params"] = params
    return out


def tokenize(text: str) -> tuple[list[Token], list[dict]]:
    tokens: list[Token] = []
    issues: list[dict] = []
    i, line, column = 0, 1, 1
    while i < len(text):
        ch = text[i]
        if ch in " \t\r":
            i += 1
            column += 1
            continue
        if ch == "\n":
            i += 1
            line += 1
            column = 1
            continue
        if ch == "?":
            start_col = column
            i += 1
            column += 1
            match = _IDENT_RE.match(text, i)
            if not match:
                issues.append(_issue("checks.rule.syntax", line, start_col,
                                     {"expected": "variable name after ?"}))
                i += 1
                column += 1
                continue
            value = match.group(0)
            tokens.append(Token("VAR", value, line, start_col))
            i = match.end()
            column += len(value)
            continue
        if ch == '"':
            start_line, start_col = line, column
            i += 1
            column += 1
            chars: list[str] = []
            while i < len(text):
                c = text[i]
                if c == "\\" and i + 1 < len(text):
                    chars.append(text[i + 1])
                    i += 2
                    column += 2
                    continue
                if c == '"':
                    i += 1
                    column += 1
                    break
                if c == "\n":
                    issues.append(_issue("checks.rule.syntax", start_line, start_col,
                                         {"reason": "unterminated_quote"}))
                    break
                chars.append(c)
                i += 1
                column += 1
            else:
                issues.append(_issue("checks.rule.syntax", start_line, start_col,
                                     {"reason": "unterminated_quote"}))
            tokens.append(Token("NAME", "".join(chars), start_line, start_col))
            continue
        # 关键字 / 普通名称 token：遇到空白、? 或引号结束
        start_col = column
        start = i
        while i < len(text) and text[i] not in " \t\r\n?\"\n":
            i += 1
            column += 1
        value = text[start:i]
        if value in _KEYWORDS:
            tokens.append(Token("KW", value, line, start_col))
        elif value:
            tokens.append(Token("NAME", value, line, start_col))
        else:
            tokens.append(Token("SYMBOL", value, line, start_col))
            i += 1
            column += 1
    tokens.append(Token("EOF", "", line, column))
    return tokens, issues


class _Parser:
    def __init__(self, tokens: list[Token], context: SchemaContext, rule_key: str = ""):
        self.tokens = tokens
        self.pos = 0
        self.context = context
        self.rule_key = rule_key
        self.issues: list[dict] = []

    def peek(self) -> Token:
        return self.tokens[self.pos]

    def take(self) -> Token:
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def error(self, token: Token, expected: str) -> None:
        issue = _issue("checks.rule.syntax", token.line, token.column, {"expected": expected})
        issue["target"]["id"] = self.rule_key
        self.issues.append(issue)

    def expect_kw(self, value: str) -> bool:
        token = self.peek()
        if token.kind == "KW" and token.value == value:
            self.take()
            return True
        self.error(token, value)
        return False

    def expect_name(self) -> Optional[str]:
        token = self.peek()
        if token.kind == "NAME":
            self.take()
            return token.value
        self.error(token, "name")
        return None

    def expect_var(self) -> Optional[str]:
        token = self.peek()
        if token.kind == "VAR":
            self.take()
            return token.value
        self.error(token, "?variable")
        return None

    def _relation_name(self, raw: str) -> str:
        key = self.context._key(raw)
        canonical = self.context.resolve_relation(raw)
        if canonical:
            return (self.context.relations.get(canonical) or {}).get("name") or raw
        inverse_owner = self.context.inverse_names.get(key)
        if inverse_owner:
            return (self.context.relations.get(inverse_owner) or {}).get("name") or raw
        return raw

    def _inverse_owner_key(self, raw: str) -> Optional[str]:
        return self.context.inverse_names.get(self.context._key(raw))

    def _type_name(self, raw: str) -> str:
        key = self.context.resolve_entity(raw)
        if key:
            return (self.context.entities.get(key) or {}).get("name") or raw
        return raw

    def parse_atom(self) -> Optional[dict]:
        var = self.expect_var()
        if var is None:
            return None
        token = self.peek()
        if token.kind == "KW" and token.value == "是":
            self.take()
            cls = self.expect_name()
            if cls is None:
                return None
            return {"kind": "type", "s": var, "cls": self._type_name(cls)}
        rel = self.expect_name()
        if rel is None:
            return None
        obj = self.expect_var()
        if obj is None:
            return None
        inverse_owner = self._inverse_owner_key(rel)
        if inverse_owner:
            canonical = (self.context.relations.get(inverse_owner) or {}).get("name") or rel
            return {"kind": "rel", "s": obj, "p": canonical, "o": var}
        return {"kind": "rel", "s": var, "p": self._relation_name(rel), "o": obj}

    def parse(self) -> Optional[dict]:
        if not self.expect_kw("IF"):
            return None
        premises = [self.parse_atom()]
        if premises[0] is None:
            return None
        while self.peek().kind == "KW" and self.peek().value == "AND":
            self.take()
            atom = self.parse_atom()
            if atom is None:
                return None
            premises.append(atom)
        if not self.expect_kw("THEN"):
            return None
        conclusion = self.parse_atom()
        if conclusion is None:
            return None
        if self.peek().kind != "EOF":
            self.error(self.peek(), "end of rule")
            return None
        return {"premises": premises, "conclusion": conclusion}


def parse_rule_text(
    text: str, context: SchemaContext, rule_key: str = "",
) -> tuple[Optional[dict], list[dict]]:
    text = str(text or "")
    if len(text) > MAX_RULE_TEXT_CHARS:
        issue = _issue("checks.rule.limits", 1, 1, {"max_chars": MAX_RULE_TEXT_CHARS})
        issue["target"]["id"] = rule_key
        return None, [issue]
    tokens, issues = tokenize(text)
    for issue in issues:
        issue["target"]["id"] = rule_key
    if issues:
        return None, issues
    parser = _Parser(tokens, context, rule_key=rule_key)
    ast = parser.parse()
    return ast, parser.issues


__all__ = ["MAX_RULE_TEXT_CHARS", "parse_rule_text", "tokenize"]
