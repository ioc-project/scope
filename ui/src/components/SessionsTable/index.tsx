/*
 * Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
 * SPDX-License-Identifier: Apache-2.0
 */

import { useMemo, useState } from 'react';
import { Session, CompletedSessions } from '@/types/scope.type';
import {
  useApplicationSessionsWithStatefulEval,
  useSessionsWithCognitionProtocols
} from '@/api/scopeApi';
import { hasCognitionProtocol } from '../CognitionProtocolsSummary/utils';
import { SessionsTable } from './SessionsTable.tsx';
import { Stack, SxProps, useTheme } from '@mui/material';
import { useDebouncedValue } from '@/utils';
import dayjs, { Dayjs } from 'dayjs';
import { useParams } from 'react-router-dom';

interface SessionsTablePropsWrapper {
  applicationId?: string;
  startDate: number | null;
  endDate: number | null;
  enableSearch?: boolean;
  onSessionClick?: (session: Session) => void;
  // Click on a Cognition protocol tag of a session.
  onProtocolClick?: (session: Session) => void;
  defaultHiddenColumns?: string[];
  filteredSessionIds?: string[];
  title?: string;
  showDateInterval?: boolean;
  agentId?: string;
  semanticGroupId?: string;
  containerSx?: SxProps;
}

const AGENTS = ['Assistant', 'Planner', 'Test Executor'];
const LLMs = ['GPT-4', 'Claude'];

const getRandomAgents = () => {
  const shuffled = AGENTS.sort(() => 0.5 - Math.random());

  const size = Math.floor(Math.random() * AGENTS.length) + 1;

  return shuffled.slice(0, size);
};

const getRandomLLMs = () => {
  const shuffled = LLMs.sort(() => 0.5 - Math.random());

  const size = Math.floor(Math.random() * LLMs.length) + 1;

  return shuffled.slice(0, size);
};

export const SessionsTableWrapper = ({
  applicationId,
  startDate,
  endDate,
  enableSearch = false,
  onSessionClick,
  onProtocolClick,
  defaultHiddenColumns = [],
  title,
  filteredSessionIds,
  showDateInterval = true,
  agentId,
  semanticGroupId,
  containerSx
}: SessionsTablePropsWrapper) => {
  const { applicationId: applicationIdFromParams } = useParams();

  const debouncedStartDate = useDebouncedValue(startDate, 500);
  const debouncedEndDate = useDebouncedValue(endDate, 500);
  const {
    data: sessionsData,
    error: sessionsError,
    isLoading: sessionsLoading
  } = useApplicationSessionsWithStatefulEval(
    applicationId ?? applicationIdFromParams ?? '',
    debouncedStartDate,
    debouncedEndDate,
    true,
    semanticGroupId
  );

  // Cognition (L9) protocols of the sessions of the same interval. They only
  // enrich the table: a failure here must not prevent the sessions from showing.
  const { data: cognitionProtocolsData } = useSessionsWithCognitionProtocols(
    applicationId ?? applicationIdFromParams ?? '',
    debouncedStartDate ?? undefined,
    debouncedEndDate ?? undefined
  );

  // Session id -> protocols, only for the sessions with a protocol enabled or activated.
  const cognitionProtocolsBySessionId = useMemo(() => {
    const protocolSessions = (cognitionProtocolsData?.sessions ?? []).filter(
      hasCognitionProtocol
    );
    return new Map(
      protocolSessions.map((session) => [
        session.sessionId,
        session.cognitionProtocols
      ])
    );
  }, [cognitionProtocolsData?.sessions]);

  const sessionsWithStatefulEval: CompletedSessions[] = useMemo(() => {
    if (!sessionsData?.sessionList) return [];

    const sessions = sessionsData.sessionList.map((rawSession) => {
      const session = {
        ...rawSession,
        cognitionProtocols: cognitionProtocolsBySessionId.get(
          rawSession.sessionId
        )
      };
      const statefulEval = (session as any).statefulEval;
      if (!statefulEval) return session as CompletedSessions;

      let parsedValue: any = null;
      try {
        parsedValue =
          typeof statefulEval.value === 'string'
            ? JSON.parse(statefulEval.value)
            : statefulEval;
      } catch {
        parsedValue = null;
      }

      let reasoningJson = null;
      try {
        reasoningJson =
          typeof parsedValue?.reasoning === 'string'
            ? JSON.parse(parsedValue.reasoning)
            : (parsedValue?.reasoning ?? null);
      } catch {
        reasoningJson = null;
      }

      return {
        ...session,
        statefulEval: {
          metric_id: statefulEval.metric_id,
          name: statefulEval.name,
          source: statefulEval.source,
          reasoning: statefulEval.reasoning,
          value: parsedValue?.value,
          reasoningJson
        }
      } as CompletedSessions;
    });

    const sessionsWithStatefulEvalFiltered: CompletedSessions[] =
      filteredSessionIds
        ? sessions.filter((session) =>
            filteredSessionIds.includes(session.sessionId)
          )
        : sessions;

    const sessionsWithStatefulEvalFilteredByAgent: CompletedSessions[] =
      agentId
        ? (sessionsWithStatefulEvalFiltered.filter((session) =>
            session.agents?.some((agent) => agent === agentId)
          ) ?? [])
        : sessionsWithStatefulEvalFiltered;

    return sessionsWithStatefulEvalFilteredByAgent;
  }, [
    sessionsData?.sessionList,
    cognitionProtocolsBySessionId,
    filteredSessionIds,
    agentId
  ]);

  return (
    <Stack direction="column" gap={'8px'} sx={containerSx}>
      {/*<Insights*/}
      {/*  insights={[*/}
      {/*    {*/}
      {/*      value: '74%',*/}
      {/*      description:*/}
      {/*        'of failed sessions in the last evaluation run showed early signs of Planner role drift, where the Planner agent either generated overly verbose goals or attempted to execute tasks instead of delegating'*/}
      {/*    }*/}
      {/*  ]}*/}
      {/*/>*/}

      {/* {enableSearch && (
        <SearchField
          placeholder="Search by session name, id or external id"
          value={queryFilter}
          onChangeCallback={setQueryFilter}
          sx={{ '& .MuiInputBase-root': { marginTop: 0, width: '444px', height: '36px' } }}
        />
      )} */}

      <SessionsTable
        data={sessionsWithStatefulEval}
        isLoading={sessionsLoading}
        isError={Boolean(sessionsError)}
        startDate={startDate ? dayjs.unix(startDate) : null}
        endDate={endDate ? dayjs.unix(endDate) : null}
        onSessionClick={onSessionClick}
        onProtocolClick={onProtocolClick}
        // onReload={() => executeQuery()}
        defaultHiddenColumns={defaultHiddenColumns}
        title={title}
        showDateInterval={showDateInterval}
      />
    </Stack>
  );
};
