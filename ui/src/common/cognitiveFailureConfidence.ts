/*
 * Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
 * SPDX-License-Identifier: Apache-2.0
 */

import { TagBackgroundColorVariants } from '@open-ui-kit/core';
import { CognitiveFailure } from '@/types/scope.type';

// Same colors as the priority tags, by the confidence of a cognitive failure.
export const CONFIDENCE_COLORS: Record<string, TagBackgroundColorVariants> = {
  high: TagBackgroundColorVariants.AccentFWeak,
  medium: TagBackgroundColorVariants.AccentEWeak,
  low: TagBackgroundColorVariants.AccentGWeak
};

export const CONFIDENCE_LABELS: Record<string, string> = {
  high: 'High',
  medium: 'Medium',
  low: 'Low'
};

const CONFIDENCE_ORDER: Record<string, number> = {
  high: 3,
  medium: 2,
  low: 1
};

// Highest confidence first, then by code.
export const compareFailures = (
  a: CognitiveFailure,
  b: CognitiveFailure
): number =>
  (CONFIDENCE_ORDER[b.confidence] ?? 0) -
    (CONFIDENCE_ORDER[a.confidence] ?? 0) || a.name.localeCompare(b.name);
