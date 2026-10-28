/*
 * Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
 * SPDX-License-Identifier: Apache-2.0
 */

import { useMemo } from 'react';
import { Stack, Typography } from '@mui/material';
import { WidgetCard } from '../WidgetCard/WidgetCard';
import { useSessionsWithCognitionProtocols } from '@/api/scopeApi';
import { getCohortDescription, groupIntoCohorts } from './utils';

interface CognitionProtocolsSummaryProps {
  applicationId: string;
  startDate?: number | null;
  endDate?: number | null;
}

// Cards for the Collect tab: one per group of sessions, grouped by the
// Cognition (L9) protocols activated in them, with how many sessions it holds.
// It loads its own data and renders nothing until there is some, so it can sit
// in a row next to other cards without ever blocking them.
export const CognitionProtocolsSummary = ({
  applicationId,
  startDate,
  endDate
}: CognitionProtocolsSummaryProps) => {
  const { data } = useSessionsWithCognitionProtocols(
    applicationId,
    startDate ?? undefined,
    endDate ?? undefined
  );

  const sessions = useMemo(() => data?.sessions ?? [], [data?.sessions]);
  const cohorts = useMemo(() => groupIntoCohorts(sessions), [sessions]);

  return (
    <>
      {cohorts.map((cohort) => {
        const size = cohort.sessions.length;
        return (
          <WidgetCard
            key={cohort.label}
            title={cohort.label}
            description={getCohortDescription(cohort)}
            content={
              <Stack direction="row" gap="4px" alignItems="baseline">
                <Typography variant={'h5'}>{size}</Typography>
                <Typography variant={'caption'}>
                  {`${size === 1 ? 'session' : 'sessions'} (${Math.round(
                    (size / sessions.length) * 100
                  )}%)`}
                </Typography>
              </Stack>
            }
          />
        );
      })}
    </>
  );
};

export default CognitionProtocolsSummary;
