/*
 * Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
 * SPDX-License-Identifier: Apache-2.0
 */

import { Banner, Skeleton } from '@open-ui-kit/core';
import { CognitiveAssessment, CognitiveFailure } from '@/types/scope.type';

interface CognitiveFailuresBannerProps {
  // Undefined while there is no data for the session.
  failures?: CognitiveFailure[];
  assessment?: CognitiveAssessment;
  isLoading?: boolean;
  isError?: boolean;
}

// A summary of the cognitive failures detected for a session.
export const CognitiveFailuresBanner = ({
  failures,
  assessment,
  isLoading = false,
  isError = false
}: CognitiveFailuresBannerProps) => {
  if (isLoading) {
    return <Skeleton variant="rounded" height={48} />;
  }

  if (isError) {
    return (
      <Banner
        status="negative"
        text="Failed to load cognitive observability data."
      />
    );
  }

  if (!failures) return null;

  if (assessment?.status !== 'complete') {
    const state = assessment?.status;
    const title =
      state === 'in_progress'
        ? 'Cognitive assessment in progress.'
        : state === 'incomplete'
          ? 'Cognitive assessment incomplete.'
          : state === 'error'
            ? 'Cognitive assessment failed.'
            : 'Cognitive assessment status unavailable. Reassess this session.';
    const coverage = assessment?.requiredEvidence
      ? ` Evidence delivered to reviewer: ${assessment.reviewedEvidence}/${assessment.requiredEvidence}.`
      : '';
    return (
      <Banner
        status="warning"
        text={`${title} ${assessment?.reason ?? ''}${coverage}`.trim()}
      />
    );
  }

  const highConfidenceFailures = failures.filter(
    (failure) => failure.confidence === 'high'
  ).length;

  return (
    <Banner
      status={
        highConfidenceFailures > 0
          ? 'warning'
          : failures.length > 0
            ? 'info'
            : 'success'
      }
      text={
        failures.length === 0
          ? 'No cognitive failures detected in the reviewed captured evidence.'
          : `${failures.length} cognitive failure${
              failures.length === 1 ? '' : 's'
            } detected, ${highConfidenceFailures} with high confidence.`
      }
    />
  );
};
