/*
 * Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
 * SPDX-License-Identifier: Apache-2.0
 */

import {
  MaterialReactTable,
  MRT_SortingState,
  MRT_VisibilityState
} from 'material-react-table';
import {
  CreateTableInstance,
  EmptyState,
  GeneralSize,
  TableProps,
  Tag,
  TagBackgroundColorVariants,
  Tooltip
} from '@open-ui-kit/core';
import { useMemo, useState } from 'react';
import { Box, Stack, Typography } from '@mui/material';
import { format } from 'date-fns';
import { SessionInsightRow } from '@/types/scope.type';
import { Tags } from '@/components';
import {
  getCognitiveFailureLabel,
  getCognitiveFailureTaxonomy
} from '@/common/cognitiveFailureTaxonomy';
import {
  CONFIDENCE_COLORS,
  CONFIDENCE_LABELS
} from '@/common/cognitiveFailureConfidence';
import { formatDurationMs } from '@/utils/stringUtils';
import { GLOBAL_BACKGROUND_COLOR } from '@/common/styles';
import { ExecutionTrace } from '@/components/ExecutionTrace';
import { colorTokens } from '@/theme/colors';
import { PATHS } from '@/routes/routes';
import { useNavigate, useParams } from 'react-router-dom';

type MRT_ColumnDefList = TableProps<SessionInsightRow>['columns'];

interface SessionsInsightsTableProps {
  data: SessionInsightRow[];
  isLoading: boolean;
  onSessionClick?: (session: SessionInsightRow) => void;
}

export const SessionsInsightsTable = ({
  data,
  isLoading,
  onSessionClick
}: SessionsInsightsTableProps) => {
  const [columnVisibility, setColumnVisibility] = useState<MRT_VisibilityState>(
    { labels: false, duration: false }
  );
  const [sorting, setSorting] = useState<MRT_SortingState>([
    { id: 'priority', desc: true }
  ]);

  const columns = useMemo<MRT_ColumnDefList>(
    () => [
      {
        accessorKey: 'sessionId',
        header: 'ID',
        size: 108,
        accessorFn: ({ sessionId }) => (
          <Tooltip title={sessionId} placement={'top'}>
            <Typography
              variant={'body2'}
              sx={{
                textOverflow: 'ellipsis',
                overflow: 'hidden',
                whiteSpace: 'nowrap'
              }}
            >
              {sessionId}
            </Typography>
          </Tooltip>
        )
      },
      {
        accessorKey: 'name',
        header: 'Insight',
        size: 280,
        accessorFn: ({ name, cognitiveFailure }) => {
          // A cognitive failure: a tag with its title, code and confidence
          // level; the tooltip is the general description of the failure.
          if (cognitiveFailure) {
            const label = `${getCognitiveFailureLabel(cognitiveFailure.name)}: ${
              CONFIDENCE_LABELS[cognitiveFailure.confidence] ??
              cognitiveFailure.confidence
            }`;
            return (
              <Tooltip
                title={
                  getCognitiveFailureTaxonomy(cognitiveFailure.name)?.summary ??
                  label
                }
                placement={'top'}
              >
                <Box>
                  <Tag
                    color={
                      CONFIDENCE_COLORS[cognitiveFailure.confidence] ??
                      TagBackgroundColorVariants.AccentAWeak
                    }
                    size={GeneralSize.Medium}
                  >
                    {label}
                  </Tag>
                </Box>
              </Tooltip>
            );
          }

          return (
            <Tooltip title={name} placement={'top'}>
              <Typography
                variant={'body2'}
                sx={{
                  textOverflow: 'ellipsis',
                  overflow: 'hidden',
                  whiteSpace: 'nowrap'
                }}
              >
                {name}
              </Typography>
            </Tooltip>
          );
        }
      },
      {
        accessorKey: 'description',
        header: 'Description',
        size: 150,
        accessorFn: ({ description }) => (
          <Tooltip title={description} placement={'top'}>
            <Typography
              variant={'body2'}
              sx={{
                textOverflow: 'ellipsis',
                overflow: 'hidden',
                whiteSpace: 'nowrap'
              }}
            >
              {description}
            </Typography>
          </Tooltip>
        )
      },
      {
        accessorKey: 'priority',
        header: 'Priority',
        size: 120,
        accessorFn: ({ priority }) => {
          // Cognitive failure rows have no priority.
          if (!priority) return null;
          const colorMap: Record<string, TagBackgroundColorVariants> = {
            high: TagBackgroundColorVariants.AccentFWeak,
            medium: TagBackgroundColorVariants.AccentEWeak,
            low: TagBackgroundColorVariants.AccentGWeak
          };
          return (
            <Tag
              color={
                colorMap[priority?.toLowerCase()] ??
                TagBackgroundColorVariants.AccentAWeak
              }
              size={GeneralSize.Medium}
            >
              {priority}
            </Tag>
          );
        },
        sortingFn: (rowA, rowB) => {
          const priorityOrder: Record<string, number> = {
            high: 3,
            medium: 2,
            low: 1
          };
          const a = priorityOrder[rowA.original.priority?.toLowerCase()] ?? 0;
          const b = priorityOrder[rowB.original.priority?.toLowerCase()] ?? 0;
          return a - b;
        }
      },
      {
        accessorKey: 'labels',
        header: 'Labels',
        size: 180,
        accessorFn: ({ labels }) => (
          <Tags
            tags={(labels ?? []).map((label) => ({
              name: label,
              color: TagBackgroundColorVariants.AccentAWeak
            }))}
            minDisplayed={2}
          />
        )
      },
      {
        accessorKey: 'duration',
        header: 'Duration',
        size: 120,
        accessorFn: ({ duration, cognitiveFailure }) =>
          cognitiveFailure ? null : (
            <Typography variant={'body2'}>
              {formatDurationMs(duration)}
            </Typography>
          )
      },
      {
        accessorKey: 'createdAt',
        header: 'Created',
        size: 140,
        accessorFn: ({ createdAt }) => (
          <Typography variant={'body2'}>
            {createdAt ? format(new Date(createdAt), 'MMM d, yyyy HH:mm') : ''}
          </Typography>
        ),
        sortingFn: (rowA, rowB) => {
          const a = new Date(rowA.original.createdAt).getTime() || 0;
          const b = new Date(rowB.original.createdAt).getTime() || 0;
          return a - b;
        }
      }
    ],
    []
  );

  const tableRef = CreateTableInstance({
    data,
    columns,
    isLoading,
    rowCount: data?.length ?? 0,
    title: { label: 'Sessions Insights' },
    topToolbarProps: {
      export: { enableExport: false }
    },
    enableSorting: true,
    enableColumnResizing: true,
    enableRowActions: true,
    renderEmptyRowsFallback: () => (
      <EmptyState
        title={'No Sessions Insights'}
        description={'No insights found for this group'}
      />
    ),

    state: { columnVisibility, sorting },
    onColumnVisibilityChange: setColumnVisibility,
    onSortingChange: setSorting,

    muiTableBodyRowProps: ({ row }) => {
      return {
        sx: {
          cursor: 'pointer'
        },
        onClick: () => {
          onSessionClick?.(row.original);
        }
      };
    }
  });

  return <MaterialReactTable table={tableRef} />;
};
