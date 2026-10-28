/*
 * Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
 * SPDX-License-Identifier: Apache-2.0
 */

import type {
  SessionInsightRow,
  SessionWithCognitiveObservability
} from '@/types/scope.type';
import { compareFailures } from '@/common/cognitiveFailureConfidence';

const toIsoDate = (unixSeconds: string): string => {
  const date = new Date(Number(unixSeconds) * 1000);
  return Number.isNaN(date.getTime()) ? '' : date.toISOString();
};

// One row per cognitive failure, shaped like a session insight: the session id
// is repeated for every failure of a session, `name` is the failure code and
// `description` the detector's reasoning.
export const toCognitiveFailureRows = (
  sessions: SessionWithCognitiveObservability[] | undefined
): SessionInsightRow[] =>
  (sessions ?? []).flatMap((session) => {
    const createdAt = toIsoDate(session.timestamp);
    return [...(session.cognitiveFailures ?? [])]
      .sort(compareFailures)
      .map(
        (failure): SessionInsightRow => ({
          sessionId: session.sessionId,
          insightId: `${session.sessionId}:${failure.name}`,
          name: failure.name,
          description: failure.reasoning,
          priority: '',
          labels: [],
          duration: 0,
          startTime: createdAt,
          endTime: createdAt,
          createdAt,
          targetNodeId: '',
          cognitiveFailure: failure
        })
      );
  });
