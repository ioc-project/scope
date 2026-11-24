/*
 * Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Turns any value coming from a trace (a string, JSON, JSON inside a string,
 * several JSON documents glued together, arrays, nested objects...) into a
 * small tree that a component can render cleanly.
 *
 * It is purely structural: nothing here knows about a particular framework or
 * message schema. The rules are:
 *  - a string that is valid JSON is replaced by what it decodes to, again and
 *    again (a JSON string holding JSON holding a string...);
 *  - a string made of several JSON documents (`{...}\n[...]`) becomes a list;
 *  - objects become key/value entries, arrays become lists;
 *  - any other string is text, with escaped newlines (`\n` written as two
 *    characters) turned into real ones.
 */

export type ContentNode =
  | { kind: 'text'; text: string }
  | { kind: 'scalar'; text: string }
  | { kind: 'array'; items: ContentNode[] }
  | { kind: 'object'; entries: { key: string; node: ContentNode }[] };

const MAX_DEPTH = 8;

/** Split `s` into top-level JSON texts if it is only made of JSON values. */
const splitJsonDocuments = (s: string): string[] | null => {
  const docs: string[] = [];
  let i = 0;
  while (i < s.length) {
    while (i < s.length && /\s/.test(s[i])) i++;
    if (i >= s.length) break;

    const start = i;
    const open = s[i];
    if (open === '{' || open === '[') {
      let depth = 0;
      let inString = false;
      for (; i < s.length; i++) {
        const c = s[i];
        if (inString) {
          if (c === '\\') i++;
          else if (c === '"') inString = false;
        } else if (c === '"') inString = true;
        else if (c === '{' || c === '[') depth++;
        else if (c === '}' || c === ']') {
          depth--;
          if (depth === 0) {
            i++;
            break;
          }
        }
      }
      if (depth !== 0) return null; // unbalanced: not JSON
    } else if (open === '"') {
      i++;
      for (; i < s.length; i++) {
        if (s[i] === '\\') i++;
        else if (s[i] === '"') {
          i++;
          break;
        }
      }
    } else {
      return null; // a bare word/number among documents: treat as text
    }
    docs.push(s.slice(start, i));
  }
  return docs.length > 0 ? docs : null;
};

/** Parse `s` as one or more JSON documents. `undefined` if it is not JSON. */
const parseJsonDocuments = (s: string): unknown[] | undefined => {
  const trimmed = s.trim();
  const first = trimmed[0];
  if (first !== '{' && first !== '[' && first !== '"') return undefined;
  try {
    return [JSON.parse(trimmed)];
  } catch {
    // fall through to the multi-document attempt
  }
  const parts = splitJsonDocuments(trimmed);
  if (!parts || parts.length < 2) return undefined;
  try {
    return parts.map((part) => JSON.parse(part));
  } catch {
    return undefined;
  }
};

/** `a\nb` written with a literal backslash-n (no real newline) -> real newlines. */
const unescapeLiteralNewlines = (s: string): string => {
  if (s.includes('\n') || !/\\[nrt]/.test(s)) return s;
  return s
    .replace(/\\r\\n/g, '\n')
    .replace(/\\n/g, '\n')
    .replace(/\\t/g, '\t')
    .replace(/\\"/g, '"');
};

const fromString = (s: string, depth: number): ContentNode => {
  if (depth < MAX_DEPTH) {
    const docs = parseJsonDocuments(s);
    if (docs) {
      const nodes = docs.map((doc) => fromValue(doc, depth + 1));
      return nodes.length === 1 ? nodes[0] : { kind: 'array', items: nodes };
    }
  }
  return { kind: 'text', text: unescapeLiteralNewlines(s).trim() };
};

const fromValue = (value: unknown, depth: number): ContentNode => {
  if (typeof value === 'string') return fromString(value, depth);
  if (value === null || value === undefined) {
    return { kind: 'scalar', text: 'null' };
  }
  if (typeof value !== 'object') {
    return { kind: 'scalar', text: String(value) };
  }
  if (depth >= MAX_DEPTH) {
    return { kind: 'text', text: JSON.stringify(value, null, 2) };
  }
  if (Array.isArray(value)) {
    return {
      kind: 'array',
      items: value.map((item) => fromValue(item, depth + 1))
    };
  }
  return {
    kind: 'object',
    entries: Object.entries(value as Record<string, unknown>).map(
      ([key, v]) => ({ key, node: fromValue(v, depth + 1) })
    )
  };
};

export const parseContent = (raw: unknown): ContentNode => fromValue(raw, 0);

/** True when there is nothing worth displaying. */
export const isEmptyContent = (node: ContentNode): boolean => {
  switch (node.kind) {
    case 'text':
      return node.text === '';
    case 'array':
      return node.items.every(isEmptyContent);
    case 'object':
      return node.entries.every((entry) => isEmptyContent(entry.node));
    default:
      return false;
  }
};
