/*
 * Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
 * SPDX-License-Identifier: Apache-2.0
 */

import { useMemo } from 'react';
import { Stack, Typography } from '@open-ui-kit/core';
import { Box, LinearProgress, useTheme } from '@mui/material';
import InfoOutlineIcon from '@mui/icons-material/InfoOutline';
import { CustomTooltip } from '@/components/CustomTooltip';
import { WidgetCard } from '@/components/WidgetCard/WidgetCard';
import {
  getCognitiveFailureLabel,
  getCognitiveFailureTaxonomy
} from '@/common/cognitiveFailureTaxonomy';
import { SessionWithCognitiveObservability } from '@/types/scope.type';
import { getScoreColor } from '@/utils/metrics';

interface FailuresOverviewProps {
  sessions?: SessionWithCognitiveObservability[];
  selectedFailure?: string | null;
  onSelectFailure?: (failure: string | null) => void;
}

// A thin progress bar showing the share of sessions affected.
const RatioBar = ({ ratio, color }: { ratio: number; color: string }) => {
  const theme = useTheme();
  return (
    <LinearProgress
      variant="determinate"
      value={Math.min(100, Math.max(0, ratio * 100))}
      sx={{
        width: '100%',
        height: 6,
        borderRadius: 3,
        backgroundColor: theme.palette.vars.controlBackgroundMedium,
        '& .MuiLinearProgress-bar': {
          backgroundColor: color,
          borderRadius: 3
        }
      }}
    />
  );
};

// The share of sessions with cognitive failures, and one card per failure.
// Selecting a card filters the table below to that failure.
export const FailuresOverview = ({
  sessions,
  selectedFailure = null,
  onSelectFailure
}: FailuresOverviewProps) => {
  const theme = useTheme();

  const stats = useMemo(() => {
    const totalSessions = sessions?.length ?? 0;
    const counts = new Map<string, number>();
    let sessionsWithFailure = 0;

    (sessions ?? []).forEach((session) => {
      const names = new Set(
        (session.cognitiveFailures ?? []).map((failure) => failure.name)
      );
      if (names.size > 0) sessionsWithFailure += 1;
      // Each failure counts at most once per session.
      names.forEach((name) => counts.set(name, (counts.get(name) ?? 0) + 1));
    });

    const failures = Array.from(counts, ([name, count]) => ({
      name,
      count,
      ratio: totalSessions > 0 ? count / totalSessions : 0
    })).sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));

    return {
      failures,
      totalSessions,
      sessionsWithFailure,
      sessionsWithFailureRatio:
        totalSessions > 0 ? sessionsWithFailure / totalSessions : 0
    };
  }, [sessions]);

  const primary = theme.palette.vars.interactivePrimaryDefaultDefault;

  return (
    <Stack direction="column" gap="8px">
      <Stack direction="row" gap="8px" alignItems="flex-start">
        <Typography variant={'h6'}>Overview</Typography>
        <CustomTooltip
          title={
            <Typography variant={'caption'}>
              Select a failure to filter the table below.
            </Typography>
          }
          placement={'top'}
          sx={{ maxWidth: '550px' }}
        >
          <InfoOutlineIcon
            sx={{ width: '14px', height: '14px', cursor: 'pointer' }}
          />
        </CustomTooltip>
      </Stack>

      {/* Summary card on its own row */}
      <Stack direction="row" sx={{ width: '100%' }}>
        <WidgetCard
          title={'Sessions with failures'}
          description={
            'The percentage of sessions with at least one cognitive failure.'
          }
          content={
            <Stack direction="column" gap="8px" sx={{ width: '100%' }}>
              <Stack direction="row" gap="4px" alignItems="baseline">
                <Typography variant={'h6'}>
                  {`${Math.round(stats.sessionsWithFailureRatio * 100)}%`}
                </Typography>
                <Typography variant={'caption'}>
                  {`(${stats.sessionsWithFailure} / ${stats.totalSessions} sessions)`}
                </Typography>
              </Stack>
              {/* Few sessions with failures is the good case. */}
              <RatioBar
                ratio={stats.sessionsWithFailureRatio}
                color={getScoreColor(
                  100 - stats.sessionsWithFailureRatio * 100,
                  theme
                )}
              />
            </Stack>
          }
          cardSx={{ width: '350px' }}
        />
      </Stack>

      {/* One card per failure, most frequent first, in equal-width columns */}
      <Box
        sx={{
          width: '100%',
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))',
          gap: '8px'
        }}
      >
        {stats.failures.map(({ name, count, ratio }) => {
          const isSelected = selectedFailure === name;
          return (
            <WidgetCard
              key={name}
              title={getCognitiveFailureLabel(name)}
              description={getCognitiveFailureTaxonomy(name)?.summary}
              onClick={() => onSelectFailure?.(isSelected ? null : name)}
              content={
                <Stack direction="column" gap="8px" sx={{ width: '100%' }}>
                  <Typography variant={'caption'}>
                    {`${count} / ${stats.totalSessions} sessions`}
                  </Typography>
                  <RatioBar ratio={ratio} color={primary} />
                </Stack>
              }
              cardSx={{
                width: '100%',
                cursor: 'pointer',
                transition: 'outline-color 120ms, background-color 120ms',
                outline: '2px solid transparent',
                '&:hover': {
                  outline: `2px solid color-mix(in srgb, ${primary} 50%, transparent)`
                },
                ...(isSelected
                  ? {
                      outline: `2px solid ${primary}`,
                      backgroundColor: `color-mix(in srgb, ${primary} 16%, transparent)`
                    }
                  : {})
              }}
            />
          );
        })}
      </Box>
    </Stack>
  );
};
