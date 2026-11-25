/*
 * Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
 * SPDX-License-Identifier: Apache-2.0
 */

import { ReactNode } from 'react';
import { Box, Typography, alpha, useTheme } from '@mui/material';
import FormattedContent from './index';

interface IoSectionProps {
  /** Shown as the panel title: "Input", "Prompt", "Response"... */
  label: ReactNode;
  /** Decides the accent color: what goes in versus what comes out. */
  direction: 'input' | 'output';
  value: unknown;
}

/** A labeled, tinted panel so inputs and outputs are told apart at a glance. */
export const IoSection = ({ label, direction, value }: IoSectionProps) => {
  const theme = useTheme();
  const accent =
    direction === 'input'
      ? theme.palette.info.main
      : theme.palette.success.main;

  return (
    <Box
      sx={{
        borderRadius: 1,
        borderLeft: `3px solid ${accent}`,
        backgroundColor: alpha(accent, 0.08),
        px: 1.25,
        py: 1,
        mb: 1,
        '&:last-child': { mb: 0 }
      }}
    >
      <Typography
        variant="caption"
        sx={{
          display: 'block',
          mb: 0.5,
          color: accent,
          fontWeight: 700,
          letterSpacing: 0.5,
          textTransform: 'uppercase'
        }}
      >
        {label}
      </Typography>
      <FormattedContent value={value} />
    </Box>
  );
};

export default IoSection;
