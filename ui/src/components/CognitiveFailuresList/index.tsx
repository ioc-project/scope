/*
 * Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
 * SPDX-License-Identifier: Apache-2.0
 */

import { GeneralSize, Tag } from '@open-ui-kit/core';
import { Stack, Typography } from '@mui/material';
import { WidgetCard } from '@/components/WidgetCard/WidgetCard';
import {
  CONFIDENCE_COLORS,
  CONFIDENCE_LABELS
} from '@/common/cognitiveFailureConfidence';
import {
  getCognitiveFailureLabel,
  getCognitiveFailureTaxonomy
} from '@/common/cognitiveFailureTaxonomy';
import { CognitiveFailure } from '@/types/scope.type';

interface CognitiveFailuresListProps {
  // Undefined while there is no data for the session. Shown in the given
  // order.
  failures?: CognitiveFailure[];
}

// One card per cognitive failure of a session, with its confidence and the
// detector's reasoning.
export const CognitiveFailuresList = ({
  failures
}: CognitiveFailuresListProps) => {
  if (!failures) return null;

  return (
    <Stack direction="column" gap="8px">
      <Typography variant={'h6'}>Cognitive Failures</Typography>
      {failures.length === 0 ? (
        <Typography variant={'body2'}>
          No cognitive failures detected for this session.
        </Typography>
      ) : (
        <Stack direction="row" gap="8px" sx={{ flexWrap: 'wrap' }}>
          {failures.map((failure) => (
            <WidgetCard
              key={failure.name}
              title={getCognitiveFailureLabel(failure.name)}
              description={getCognitiveFailureTaxonomy(failure.name)?.summary}
              content={
                <Stack
                  direction="column"
                  gap="8px"
                  alignItems="flex-start"
                  sx={{ width: '100%' }}
                >
                  <Tag
                    color={CONFIDENCE_COLORS[failure.confidence]}
                    size={GeneralSize.Medium}
                  >
                    {`${CONFIDENCE_LABELS[failure.confidence] ?? failure.confidence} confidence`}
                  </Tag>
                  <Typography variant={'body2'}>{failure.reasoning}</Typography>
                </Stack>
              }
              cardSx={{ width: '420px' }}
            />
          ))}
        </Stack>
      )}
    </Stack>
  );
};
