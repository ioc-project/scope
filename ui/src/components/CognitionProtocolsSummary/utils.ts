/*
 * Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
 * SPDX-License-Identifier: Apache-2.0
 */

import { SessionWithCognitionProtocols } from '@/types/scope.type';

export const NO_PROTOCOL_COHORT = 'No Cognition protocol';
export const ENABLED_NOT_ACTIVATED_COHORT = 'Enabled, not activated';

// Sessions grouped by the protocols that were activated in them. Nothing is
// hardcoded: the groups come from the protocols the API reports.
export interface Cohort {
  label: string;
  // Number of activated protocols, used to order the groups.
  activatedCount: number;
  sessions: SessionWithCognitionProtocols[];
}

export const shortProtocolName = (protocol: string): string =>
  protocol.replace(/^L9-/, '');

const getActivatedProtocols = (
  session: SessionWithCognitionProtocols
): string[] =>
  (session.cognitionProtocols ?? [])
    .filter((protocol) => protocol.activated)
    .map((protocol) => shortProtocolName(protocol.protocol))
    .sort();

export const getCohortLabel = (
  session: SessionWithCognitionProtocols
): string => {
  const activated = getActivatedProtocols(session);
  if (activated.length > 0) return activated.join(' + ');
  if ((session.cognitionProtocols ?? []).some((protocol) => protocol.enabled)) {
    return ENABLED_NOT_ACTIVATED_COHORT;
  }
  return NO_PROTOCOL_COHORT;
};

// Whether any Cognition protocol was enabled or activated in the session.
export const hasCognitionProtocol = (
  session: SessionWithCognitionProtocols
): boolean =>
  (session.cognitionProtocols ?? []).some(
    (protocol) => protocol.enabled || protocol.activated
  );

const cohortRank = (cohort: Cohort): number => {
  if (cohort.label === NO_PROTOCOL_COHORT) return 0;
  if (cohort.label === ENABLED_NOT_ACTIVATED_COHORT) return 1;
  return 2;
};

export const groupIntoCohorts = (
  sessions: SessionWithCognitionProtocols[]
): Cohort[] => {
  const cohorts = new Map<string, Cohort>();

  sessions.forEach((session) => {
    const label = getCohortLabel(session);
    const cohort = cohorts.get(label) ?? {
      label,
      activatedCount: getActivatedProtocols(session).length,
      sessions: []
    };
    cohort.sessions.push(session);
    cohorts.set(label, cohort);
  });

  return Array.from(cohorts.values()).sort(
    (a, b) =>
      cohortRank(a) - cohortRank(b) ||
      a.activatedCount - b.activatedCount ||
      a.label.localeCompare(b.label)
  );
};

export const getCohortDescription = (cohort: Cohort): string => {
  if (cohort.label === NO_PROTOCOL_COHORT) {
    return 'Sessions where no Cognition protocol was enabled.';
  }
  if (cohort.activatedCount === 0) {
    return 'Sessions where a Cognition protocol was enabled but never activated.';
  }
  return 'Sessions where exactly these protocols were activated.';
};
