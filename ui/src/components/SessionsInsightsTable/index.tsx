/*
 * Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
 * SPDX-License-Identifier: Apache-2.0
 */

import { Spinner, Stack } from '@open-ui-kit/core';
import { Chip, Typography } from '@mui/material';
import { useMemo, useState } from 'react';
import {
  useSessionsInsights,
  useSessionsWithCognitiveObservability
} from '@/api/scopeApi';
import { IntervalPicker } from '@/components/IntervalPicker';
import { getCognitiveFailureLabel } from '@/common/cognitiveFailureTaxonomy';
import { FailuresOverview } from './FailuresOverview';
import { SessionsInsightsTable } from './SessionsInsightsTable';
import { toCognitiveFailureRows } from './utils';
import { SessionInsight, SessionInsightRow } from '@/types/scope.type';
import { PATHS } from '@/routes/routes';
import { useTimeRangeStore } from '@/store';
import { useNavigate, useParams } from 'react-router-dom';

// The insights and the cognitive failures of all the sessions of the interval
// selected in the interval picker.
export const SessionsInsightsTableWrapper = () => {
  const { applicationId } = useParams();
  const { startDate, endDate, setStartDate, setEndDate } = useTimeRangeStore();
  const navigate = useNavigate();

  const {
    data: sessionsInsights,
    isLoading,
    isError
  } = useSessionsInsights(applicationId ?? '', startDate, endDate);

  // The cognitive failures only enrich the table: while they load, or if they
  // fail to load, the insights are shown as they are.
  const { data: cognitiveSessions } = useSessionsWithCognitiveObservability(
    applicationId ?? '',
    startDate,
    endDate
  );

  const [selectedFailure, setSelectedFailure] = useState<string | null>(null);

  // With a failure selected in the overview, only the rows of that failure are
  // listed.
  const rows = useMemo<SessionInsightRow[]>(() => {
    const failureRows = toCognitiveFailureRows(cognitiveSessions?.sessions);
    return selectedFailure
      ? failureRows.filter((row) => row.name === selectedFailure)
      : [...(sessionsInsights ?? []), ...failureRows];
  }, [sessionsInsights, cognitiveSessions?.sessions, selectedFailure]);

  // Insights and cognitive failures both open the Analysis tab of the session,
  // which shows the cognitive failures.
  const onSessionClick = (session: SessionInsight) => {
    navigate(
      PATHS.applicationCollectSessionTab
        .replace(':applicationId', applicationId ?? '')
        .replace(':sessionId', encodeURIComponent(session.sessionId))
        .replace(':sessionTab', 'analysis')
    );
  };

  const body = () => {
    if (isLoading) {
      return (
        <Stack
          justifyContent={'center'}
          alignItems={'center'}
          sx={{ width: '100%', padding: '24px' }}
        >
          <Spinner />
        </Stack>
      );
    }

    if (isError) {
      return (
        <Typography variant={'body2'} sx={{ padding: '16px' }}>
          Error loading sessions insights
        </Typography>
      );
    }

    return (
      <SessionsInsightsTable
        data={rows}
        isLoading={isLoading}
        onSessionClick={onSessionClick}
      />
    );
  };

  return (
    <Stack direction="column" gap={'16px'}>
      <IntervalPicker
        startDate={startDate}
        endDate={endDate}
        setStartDate={setStartDate}
        setEndDate={setEndDate}
      />
      <FailuresOverview
        sessions={cognitiveSessions?.sessions}
        selectedFailure={selectedFailure}
        onSelectFailure={setSelectedFailure}
      />
      <Stack direction="column" gap="8px">
        {selectedFailure && (
          <Stack direction="row">
            <Chip
              size="small"
              label={`Filtered by: ${getCognitiveFailureLabel(selectedFailure)}`}
              onDelete={() => setSelectedFailure(null)}
              color="primary"
              variant="outlined"
            />
          </Stack>
        )}
        {body()}
      </Stack>
    </Stack>
  );
};
