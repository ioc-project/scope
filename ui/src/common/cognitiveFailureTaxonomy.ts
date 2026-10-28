/*
 * Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
 * SPDX-License-Identifier: Apache-2.0
 */

// Taxonomy of the cognitive failures, keyed by code. The API returns the code
// as the failure `name` (e.g. 'DC-4'); the title and the summary are shown in
// its place.
//
// Source: backend/workers/cognitive-failure-worker/src/cognitive_failure_worker/data/failures.json
// (`code`, `name` and `summary`). Keep in sync when the taxonomy changes there.
// The summary is the general description of the failure, not the detector's
// reasoning for a given session (that is `CognitiveFailure.description`).
export interface CognitiveFailureTaxonomyEntry {
  title: string;
  summary: string;
}

export const COGNITIVE_FAILURE_TAXONOMY: Record<
  string,
  CognitiveFailureTaxonomyEntry
> = {
  // Individual
  'IR-1': {
    title: 'Individual Representational Failure',
    summary: 'Agent loses earlier instructions as its context window fills.'
  },
  'IC-1': {
    title: 'Intention Stability Failure',
    summary: 'Agent has the right commitment but drops it under interference.'
  },
  'IC-2': {
    title: 'Plan Revision Failure',
    summary:
      'Agent has the right plan but fails to revise it when context changes.'
  },
  'IE-1': {
    title: 'Individual Metacognitive Blindness',
    summary:
      'Agent does not know what it does not know, and proceeds with false confidence.'
  },

  // Dyadic
  'DR-1': {
    title: 'Semantic and Ontological Misalignment',
    summary: 'Agents use the same word to mean different things.'
  },
  'DR-2': {
    title: 'Representational Fidelity Loss at Interface',
    summary:
      'Information is physically lost or truncated in transit — the receiver never gets what the sender produced.'
  },
  'DC-1': {
    title: 'Communication Policy Inefficiency',
    summary:
      'Agents communicate too much, too little, or with the wrong signal density.'
  },
  'DC-2': {
    title: 'Illocutionary Misidentification',
    summary:
      'Agent mistakes what kind of speech act it received — a directive treated as a suggestion.'
  },
  'DC-3': {
    title: 'Negotiation and Consensus Failure',
    summary:
      'Agents exchange positions but never issue binding proposals that create obligations.'
  },
  'DC-4': {
    title: 'Task Delegation Failure',
    summary:
      'Agent delegates a task to the wrong peer because its model of peer capabilities is inaccurate.'
  },
  'DE-1': {
    title: 'Peer Knowledge Modeling Failure',
    summary:
      "Agent routes a query to a peer that doesn't hold the knowledge, or misjudges which of several peer replies to trust."
  },

  // Collective
  'CR-1': {
    title: 'Shared Task Model Failure',
    summary:
      'Agents start with incompatible understandings of what the task requires.'
  },
  'CR-2': {
    title: 'Shared State Degradation',
    summary:
      'Shared memory becomes inconsistent because agents write to it concurrently without coordination.'
  },
  'CC-1': {
    title: 'Task Decomposition and Role Allocation Failure',
    summary:
      "The system's task or role allocation has structural gaps or conflicts — whether roles were defined wrongly or never defined at all."
  },
  'CC-2': {
    title: 'Collective Goal Coordination Failure',
    summary:
      'No mechanism exists — at design time or runtime — for agents to jointly hold and coordinate their pursuit of the collective goal.'
  },
  'CC-3': {
    title: 'Incomplete Synthesis',
    summary:
      'Orchestrator commits a final output without reconciling conflicting sub-agent results.'
  },
  'CC-4': {
    title: 'Situational State Synchronisation Failure',
    summary:
      'No mechanism broadcasts situational updates to all agents, so models of current state silently diverge during execution.'
  },
  'CE-1': {
    title: 'Verification and Epistemic Control Failure',
    summary:
      'The system commits a confident output without a collective checking step — with or without contradictions present.'
  },
  'CS-1': {
    title: 'Collective Reasoning Degradation',
    summary:
      'Agents herd toward an early confident output, suppressing independent reasoning.'
  },
  'CS-2': {
    title: 'Unformalized Coordination Commitments',
    summary:
      'Coordination works until the first perturbation, because it rested on unspoken convention.'
  }
};

// The taxonomy entry of a failure code, or undefined for an unknown code.
export const getCognitiveFailureTaxonomy = (
  code: string
): CognitiveFailureTaxonomyEntry | undefined =>
  Object.prototype.hasOwnProperty.call(COGNITIVE_FAILURE_TAXONOMY, code)
    ? COGNITIVE_FAILURE_TAXONOMY[code]
    : undefined;

// "Task Delegation Failure (DC-4)"; an unknown code is shown as it is.
export const getCognitiveFailureLabel = (code: string): string => {
  const entry = getCognitiveFailureTaxonomy(code);
  return entry ? `${entry.title} (${code})` : code;
};
