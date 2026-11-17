/*
 * Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
 * SPDX-License-Identifier: Apache-2.0
 */
import { describe, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { CognitiveFailuresBanner } from './index';
import { CognitiveAssessment } from '@/types/scope.type';

vi.mock('@open-ui-kit/core', () => ({
  Banner: ({ status, text }: { status: string; text: string }) => (
    <div data-status={status}>{text}</div>
  ),
  Skeleton: () => <div>Loading</div>
}));

const assessment = (
  status: CognitiveAssessment['status']
): CognitiveAssessment => ({
  status,
  reason: 'Evidence review status',
  requiredEvidence: 10,
  reviewedEvidence: 8
});

describe('cognitive assessment status', () => {
  it.each(['unknown', 'in_progress', 'incomplete', 'error'] as const)(
    'does not show a clean result when assessment is %s',
    (status) => {
      const html = renderToStaticMarkup(
        <CognitiveFailuresBanner
          failures={[]}
          assessment={assessment(status)}
        />
      );
      expect(html).toContain('data-status="warning"');
      expect(html).not.toContain('No cognitive failures detected');
    }
  );
  it('keeps legacy responses without assessment metadata uncertain', () => {
    const html = renderToStaticMarkup(
      <CognitiveFailuresBanner failures={[]} />
    );
    expect(html).toContain('status unavailable');
    expect(html).not.toContain('data-status="success"');
  });
  it('shows a qualified clean result only after completion', () => {
    const html = renderToStaticMarkup(
      <CognitiveFailuresBanner
        failures={[]}
        assessment={assessment('complete')}
      />
    );
    expect(html).toContain('data-status="success"');
    expect(html).toContain('reviewed captured evidence');
  });
  it('shows findings on a completed assessment', () => {
    const html = renderToStaticMarkup(
      <CognitiveFailuresBanner
        failures={[
          { name: 'IE-1', confidence: 'high', reasoning: 'Trace evidence' }
        ]}
        assessment={assessment('complete')}
      />
    );
    expect(html).toContain(
      '1 cognitive failure detected, 1 with high confidence'
    );
    expect(html).toContain('data-status="warning"');
  });
});
