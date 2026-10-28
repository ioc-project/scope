/*
 * Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
 * SPDX-License-Identifier: Apache-2.0
 */

// Cognitive metrics are hidden for now; the code is kept commented out to be
// restored later (it came from the session's Cognitive Observability tab).
// It used the `cognitiveObservabilityMetrics` of the session returned by
// `useSessionsWithCognitiveObservability`, and needed these imports:
//   import { useMemo } from 'react';
//   import { Stack, Typography } from '@mui/material';
//   import { Divider, OverflowTooltip } from '@open-ui-kit/core';
//   import CategoryOutlinedIcon from '@mui/icons-material/CategoryOutlined';
//   import { Box, LinearProgress, useTheme } from '@mui/material';
//   import { ScoreSeverityBadgeLabel } from '@/components';
//   import { COGNITIVE_METRIC_GROUPS } from '@/common/cognitiveMetricGroups';
//   import { getScoreColor } from '@/utils/metrics';
//   import { CognitiveObservabilityMetric, Unit } from '@/types/scope.type';
/*
const toScore = (metric: CognitiveObservabilityMetric): number =>
  metric.value.unit === Unit.Percentage
    ? metric.value.value * 100
    : metric.value.value;

const average = (scores: number[]): number =>
  scores.length > 0 ? scores.reduce((a, b) => a + b, 0) / scores.length : 0;

  // const theme = useTheme();

  // Metrics split into the configured groups; metrics outside every group go
  // into an extra "Other" column so none is hidden.
  const metricGroups = useMemo(() => {
    const metrics = session?.cognitiveObservabilityMetrics ?? [];
    const byName = new Map(metrics.map((m) => [m.name.toLowerCase(), m]));
    const used = new Set<string>();

    const groups = COGNITIVE_METRIC_GROUPS.map((group) => {
      const groupMetrics = group.metrics.flatMap((name) => {
        const metric = byName.get(name.toLowerCase());
        if (!metric) return [];
        used.add(name.toLowerCase());
        return [metric];
      });
      return {
        title: group.title,
        icon: group.icon,
        metrics: groupMetrics,
        score: average(groupMetrics.map(toScore))
      };
    });

    const other = metrics.filter((m) => !used.has(m.name.toLowerCase()));
    return other.length > 0
      ? [
          ...groups,
          {
            title: 'Other',
            icon: CategoryOutlinedIcon,
            metrics: other,
            score: average(other.map(toScore))
          }
        ]
      : groups;
  }, [session?.cognitiveObservabilityMetrics]);

      <Stack direction="column" gap="8px">
        <Typography variant={'h6'}>Cognitive Metrics</Typography>
        {metricGroups.every((group) => group.metrics.length === 0) ? (
          <Typography variant={'body2'}>
            No cognitive metrics available for this session.
          </Typography>
        ) : (
          <Stack direction="row" gap="24px" alignItems="stretch">
            {metricGroups.map((group, index) => (
              <Stack
                key={group.title}
                direction="row"
                gap="24px"
                sx={{ flex: 1, minWidth: 0 }}
              >
                {index > 0 && <Divider orientation="vertical" flexItem />}
                <Stack
                  direction="column"
                  gap="12px"
                  sx={{ flex: 1, minWidth: 0 }}
                >
                  <Stack
                    direction="row"
                    justifyContent="space-between"
                    alignItems="center"
                    sx={{
                      paddingBottom: '8px',
                      borderBottom: `1px solid ${theme.palette.vars.baseBorderWeak}`
                    }}
                  >
                    <Stack direction="row" alignItems="center" gap="8px">
                      <Box
                        sx={{
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'center',
                          width: 24,
                          height: 24,
                          borderRadius: '50%',
                          fontSize: 14,
                          color:
                            theme.palette.vars.interactivePrimaryDefaultDefault,
                          backgroundColor:
                            theme.palette.vars.controlBackgroundMedium
                        }}
                      >
                        <group.icon fontSize="inherit" />
                      </Box>
                      <Typography
                        variant={'caption'}
                        sx={{
                          color: theme.palette.vars.baseTextWeak,
                          textTransform: 'uppercase',
                          letterSpacing: '0.08em',
                          fontWeight: 600
                        }}
                      >
                        {group.title}
                      </Typography>
                    </Stack>
                    <ScoreSeverityBadgeLabel
                      score={group.score}
                      label={`${Math.round(group.score)}% avg`}
                    />
                  </Stack>
                  {group.metrics.length === 0 ? (
                    <Typography variant={'body2'}>No metrics.</Typography>
                  ) : (
                    group.metrics.map((metric) => {
                      const score = toScore(metric);
                      const color = getScoreColor(score, theme);
                      return (
                        <Stack key={metric.name} direction="column" gap="4px">
                          <Stack
                            direction="row"
                            justifyContent="space-between"
                            gap="8px"
                          >
                            <OverflowTooltip value={metric.name}>
                              <Typography variant={'body2'}>
                                {metric.name}
                              </Typography>
                            </OverflowTooltip>
                            <Typography variant={'body2Semibold'}>
                              {`${Math.round(score)}%`}
                            </Typography>
                          </Stack>
                          <LinearProgress
                            variant="determinate"
                            value={Math.min(100, Math.max(0, score))}
                            sx={{
                              height: 6,
                              borderRadius: 3,
                              backgroundColor:
                                theme.palette.vars.controlBackgroundMedium,
                              '& .MuiLinearProgress-bar': {
                                backgroundColor: color,
                                borderRadius: 3
                              }
                            }}
                          />
                        </Stack>
                      );
                    })
                  )}
                </Stack>
              </Stack>
            ))}
          </Stack>
        )}
      </Stack>
*/

export {};
