/*
 * Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
 * SPDX-License-Identifier: Apache-2.0
 */

import { describe, expect, it } from 'vitest';
import { isEmptyContent, parseContent } from './contentFormat';

describe('parseContent', () => {
  it('keeps plain text as text', () => {
    expect(parseContent('hello')).toEqual({ kind: 'text', text: 'hello' });
  });

  it('turns literal backslash-n into real newlines', () => {
    expect(parseContent('a\\nb')).toEqual({ kind: 'text', text: 'a\nb' });
  });

  it('decodes a JSON-encoded string', () => {
    expect(parseContent('"line1\\nline2"')).toEqual({
      kind: 'text',
      text: 'line1\nline2'
    });
  });

  it('decodes JSON nested in a JSON string, any number of times', () => {
    const twice = JSON.stringify(JSON.stringify({ a: 'x\ny' }));
    expect(parseContent(twice)).toEqual({
      kind: 'object',
      entries: [{ key: 'a', node: { kind: 'text', text: 'x\ny' } }]
    });
  });

  it('expands JSON found inside a field of an object', () => {
    const node = parseContent({ payload: '{"k": [1, 2]}' });
    expect(node).toEqual({
      kind: 'object',
      entries: [
        {
          key: 'payload',
          node: {
            kind: 'object',
            entries: [
              {
                key: 'k',
                node: {
                  kind: 'array',
                  items: [
                    { kind: 'scalar', text: '1' },
                    { kind: 'scalar', text: '2' }
                  ]
                }
              }
            ]
          }
        }
      ]
    });
  });

  it('splits several JSON documents glued together', () => {
    const node = parseContent('{"a":1}\n[2]\n{"b":"}"}');
    expect(node.kind).toBe('array');
    expect(node.kind === 'array' && node.items).toHaveLength(3);
  });

  it('is not fooled by braces and brackets inside strings', () => {
    const node = parseContent('{"a":"[ not closed"}\n{"b":"}"}');
    expect(node.kind).toBe('array');
  });

  it('falls back to text when it is not valid JSON', () => {
    expect(parseContent('{bad json')).toEqual({
      kind: 'text',
      text: '{bad json'
    });
    expect(parseContent('costs {not json} here').kind).toBe('text');
  });

  it('keeps markdown inside JSON fields as text', () => {
    const node = parseContent('{"md":"# Title\\n- a\\n- b"}');
    expect(node).toEqual({
      kind: 'object',
      entries: [
        { key: 'md', node: { kind: 'text', text: '# Title\n- a\n- b' } }
      ]
    });
  });

  it('handles null, numbers, booleans and non-string input', () => {
    expect(parseContent(null)).toEqual({ kind: 'scalar', text: 'null' });
    expect(parseContent(3)).toEqual({ kind: 'scalar', text: '3' });
    expect(parseContent([true]).kind).toBe('array');
  });

  it('does not recurse forever on deeply encoded input', () => {
    let s = '"x"';
    for (let i = 0; i < 12; i++) s = JSON.stringify(s);
    expect(() => parseContent(s)).not.toThrow();
  });
});

describe('isEmptyContent', () => {
  it('is true for blank text and empty containers', () => {
    expect(isEmptyContent(parseContent('   '))).toBe(true);
    expect(isEmptyContent(parseContent('{"a":"","b":[]}'))).toBe(true);
  });

  it('is false when something is displayable', () => {
    expect(isEmptyContent(parseContent('{"a":0}'))).toBe(false);
  });
});
