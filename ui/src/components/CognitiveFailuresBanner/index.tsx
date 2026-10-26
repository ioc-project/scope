/*
 * Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
 * SPDX-License-Identifier: Apache-2.0
 */

import { Banner, Skeleton } from '@open-ui-kit/core';
import { CognitiveFailure } from '@/types/scope.type';

interface CognitiveFailuresBannerProps {
  // Undefined while there is no data for the session.
  failures?: CognitiveFailure[];
  isLoading?: boolean;
  isError?: boolean;
}

// A summary of the cognitive failures detected for a session.
export const CognitiveFailuresBanner = ({
  failures,
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
          ? 'No cognitive failures detected for this session.'
          : `${failures.length} cognitive failure${
              failures.length === 1 ? '' : 's'
            } detected, ${highConfidenceFailures} with high confidence.`
      }
    />
  );
};
