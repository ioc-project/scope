/*
 * Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
 * SPDX-License-Identifier: Apache-2.0
 */

import ForumOutlinedIcon from '@mui/icons-material/ForumOutlined';
import HubOutlinedIcon from '@mui/icons-material/HubOutlined';
import TaskAltOutlinedIcon from '@mui/icons-material/TaskAltOutlined';
import type { SvgIconComponent } from '@mui/icons-material';

// Groups of cognitive metrics, shown as columns, in display order.
export const COGNITIVE_METRIC_GROUPS: {
  title: string;
  icon: SvgIconComponent;
  metrics: string[];
}[] = [
  {
    title: 'Coordination structure',
    icon: HubOutlinedIcon,
    metrics: ['Policy Safety', 'Delegation Accuracy', 'Goal Alignment']
  },
  {
    title: 'Interaction quality',
    icon: ForumOutlinedIcon,
    metrics: [
      'Instruction Following',
      'Handoff Quality',
      'Semantic Consistency',
      'Context Preservation',
      'Confidence Calibration',
      'Verification Quality',
      'Communication Efficiency'
    ]
  },
  {
    title: 'Outcome quality',
    icon: TaskAltOutlinedIcon,
    metrics: ['Groundedness', 'Task Completion', 'Constraint Satisfaction']
  }
];
