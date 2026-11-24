/*
 * Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
 * SPDX-License-Identifier: Apache-2.0
 */

import { ReactNode, useMemo } from 'react';
import ReactMarkdown, { Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Box, Stack, Typography, useTheme } from '@mui/material';
import {
  ContentNode,
  isEmptyContent,
  parseContent
} from '@/utils/contentFormat';

/**
 * Renders any value (text, markdown, JSON, JSON inside strings, lists...) as
 * readable content. See `parseContent` for how the value is interpreted.
 *
 * Text is rendered as markdown by react-markdown, which does not render raw
 * HTML, so model output cannot inject markup.
 */

// ── text (markdown) ──────────────────────────────────────────────────────────

const TextBlock = ({ text }: { text: string }) => {
  const theme = useTheme();
  const codeBg = theme.palette.action.hover;
  const valueColor = theme.palette.text.primary;
  const body = {
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-word',
    color: valueColor,
    m: 0
  } as const;

  const components: Components = {
    p: ({ children }) => (
      <Typography variant="body2" sx={body}>
        {children}
      </Typography>
    ),
    li: ({ children }) => (
      <Typography component="li" variant="body2" sx={body}>
        {children}
      </Typography>
    ),
    h1: ({ children }) => (
      <Typography
        variant="subtitle1"
        sx={{ fontWeight: 600, color: valueColor }}
      >
        {children}
      </Typography>
    ),
    h2: ({ children }) => (
      <Typography
        variant="subtitle1"
        sx={{ fontWeight: 600, color: valueColor }}
      >
        {children}
      </Typography>
    ),
    h3: ({ children }) => (
      <Typography
        variant="subtitle2"
        sx={{ fontWeight: 600, color: valueColor }}
      >
        {children}
      </Typography>
    ),
    h4: ({ children }) => (
      <Typography
        variant="subtitle2"
        sx={{ fontWeight: 600, color: valueColor }}
      >
        {children}
      </Typography>
    ),
    h5: ({ children }) => (
      <Typography
        variant="subtitle2"
        sx={{ fontWeight: 600, color: valueColor }}
      >
        {children}
      </Typography>
    ),
    h6: ({ children }) => (
      <Typography
        variant="subtitle2"
        sx={{ fontWeight: 600, color: valueColor }}
      >
        {children}
      </Typography>
    ),
    ul: ({ children }) => (
      <Box component="ul" sx={{ m: 0, pl: 3 }}>
        {children}
      </Box>
    ),
    ol: ({ children }) => (
      <Box component="ol" sx={{ m: 0, pl: 3 }}>
        {children}
      </Box>
    ),
    pre: ({ children }) => (
      <Box
        component="pre"
        sx={{
          m: 0,
          p: 1,
          borderRadius: 1,
          overflow: 'auto',
          backgroundColor: codeBg,
          fontFamily: 'monospace',
          fontSize: '0.8125rem'
        }}
      >
        {children}
      </Box>
    ),
    code: ({ children }) => (
      <code
        style={{
          fontFamily: 'monospace',
          background: codeBg,
          borderRadius: 3,
          padding: '0 3px'
        }}
      >
        {children}
      </code>
    ),
    // Links open in a new tab and never leak the opener.
    a: ({ href, children }) => (
      <a href={href} target="_blank" rel="noopener noreferrer">
        {children}
      </a>
    ),
    blockquote: ({ children }) => (
      <Box
        component="blockquote"
        sx={{
          m: 0,
          pl: 1.5,
          borderLeft: `3px solid ${theme.palette.divider}`,
          color: theme.palette.text.secondary
        }}
      >
        {children}
      </Box>
    ),
    table: ({ children }) => (
      <Box sx={{ overflow: 'auto' }}>
        <Box
          component="table"
          sx={{
            borderCollapse: 'collapse',
            '& th, & td': {
              border: `1px solid ${theme.palette.divider}`,
              px: 1,
              py: 0.5,
              textAlign: 'left',
              fontSize: '0.875rem'
            }
          }}
        >
          {children}
        </Box>
      </Box>
    )
  };

  return (
    <Stack direction="column" gap={0.75}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {text}
      </ReactMarkdown>
    </Stack>
  );
};

// ── structured data ──────────────────────────────────────────────────────────

/** Short enough to sit on the same line as its key. */
const isInline = (node: ContentNode): boolean =>
  node.kind === 'scalar' ||
  (node.kind === 'text' && !node.text.includes('\n') && node.text.length <= 80);

const Node = ({ node }: { node: ContentNode }): ReactNode => {
  const theme = useTheme();
  const nested = {
    pl: 1.25,
    borderLeft: `2px solid ${theme.palette.divider}`
  };

  switch (node.kind) {
    case 'text':
      return <TextBlock text={node.text} />;
    case 'scalar':
      return (
        <Typography
          variant="body2"
          sx={{ fontFamily: 'monospace', color: theme.palette.text.primary }}
        >
          {node.text}
        </Typography>
      );
    case 'array': {
      const items = node.items.filter((item) => !isEmptyContent(item));
      if (items.length === 0) return null;
      return (
        <Stack
          direction="column"
          gap={0.75}
          sx={items.length > 1 ? nested : undefined}
        >
          {items.map((item, i) => (
            <Node key={i} node={item} />
          ))}
        </Stack>
      );
    }
    case 'object': {
      const entries = node.entries.filter(
        (entry) => !isEmptyContent(entry.node)
      );
      if (entries.length === 0) return null;
      return (
        <Stack direction="column" gap={0.75}>
          {entries.map(({ key, node: value }) => {
            const label = (
              <Typography
                variant="caption"
                sx={{
                  // Keys are dimmed so they read as labels next to the white values.
                  color: theme.palette.text.secondary,
                  opacity: 0.7,
                  fontWeight: 600,
                  textTransform: 'uppercase',
                  flexShrink: 0
                }}
              >
                {key}
              </Typography>
            );
            return isInline(value) ? (
              <Stack key={key} direction="row" gap={1} alignItems="baseline">
                {label}
                <Node node={value} />
              </Stack>
            ) : (
              <Box key={key}>
                {label}
                <Box sx={value.kind === 'text' ? undefined : nested}>
                  <Node node={value} />
                </Box>
              </Box>
            );
          })}
        </Stack>
      );
    }
  }
};

export const FormattedContent = ({ value }: { value: unknown }) => {
  const node = useMemo(() => parseContent(value), [value]);
  if (isEmptyContent(node)) return null;
  return <Node node={node} />;
};

export default FormattedContent;
