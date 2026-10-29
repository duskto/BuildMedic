import { z } from "zod";
export const lifecycleSchema = z.enum([
  "queued",
  "running",
  "completed",
  "failed",
  "interrupted",
  "cancelling",
  "cancelled",
]);
export const phaseSchema = z.enum([
  "ingest",
  "preflight",
  "reproduce",
  "diagnose",
  "refine",
  "validate",
  "report",
]);
export const eventSchema = z
  .object({
    task_id: z.string(),
    seq: z.number().int().nonnegative(),
    state_version: z.number().int(),
    at: z.number(),
  })
  .passthrough();
const countsSchema = z
  .object({
    collected: z.number(),
    passed: z.number(),
    failed: z.number(),
    errors: z.number(),
    skipped: z.number(),
  })
  .passthrough();
export const executionSchema = z
  .object({
    id: z.string(),
    check_id: z.string().optional(),
    lifecycle: z.string().optional(),
    status: z.string().optional(),
    passed: z.boolean().optional(),
    reason: z.string().optional(),
    exit_code: z.number().nullable().optional(),
    command: z.array(z.string()).optional(),
    counts: countsSchema.nullable().optional(),
    stdout_artifact_id: z.string().optional(),
    environment_fingerprint: z.string().optional(),
    tests_sha256: z.string().optional(),
    image_digest: z.string().nullable().optional(),
    duration_seconds: z.number().optional(),
    isolation: z.string().optional(),
    termination: z
      .object({
        cause: z.string(),
        cli_exit_code: z.number().nullable(),
        container_exit_code: z.number().nullable(),
        oom_killed: z.boolean().nullable(),
        signal: z.number().nullable(),
        possible_signal: z.number().nullable(),
        container_status: z.string().nullable(),
        state_confirmed: z.boolean(),
        cleanup_confirmed: z.boolean(),
        cleanup_error: z.string().nullable(),
      })
      .optional(),
    resource_limits: z
      .object({
        wall_seconds: z.number(),
        output_bytes: z.number(),
        memory: z.string().nullable(),
        memory_swap: z.string().nullable(),
        cpus: z.number().nullable(),
        pids: z.number().nullable(),
      })
      .optional(),
  })
  .passthrough();
export const candidateSchema = z.object({
  id: z.string(),
  rollout_id: z.string(),
  patch_artifact_id: z.string(),
  patch_hash: z.string(),
  explanation: z.string(),
  files: z.array(z.string()),
  selected: z.boolean(),
});
export const validationSchema = z
  .object({
    id: z.string(),
    candidate_id: z.string(),
    kind: z.string(),
    status: z.string(),
    target: executionSchema.optional(),
    regression: executionSchema.optional(),
    reasons: z.array(z.string()).optional(),
    scope: z.string().optional(),
  })
  .passthrough();
export const modelCallSchema = z.object({
  id: z.string(),
  status: z.string(),
  started_at: z.number(),
  finished_at: z.number().optional(),
  model: z.string(),
  provider: z.string(),
  response_model: z.string().optional(),
  finish_reason: z.string().optional(),
  total_tokens: z.number().nullable(),
  usage_known: z.boolean(),
  reserved_tokens: z.number(),
  error_code: z.string().optional(),
  rejection_reason: z.string().optional(),
  role: z.string().optional(),
  label: z.string().optional(),
  harness_hash: z.string().optional(),
});
export const harnessSchema = z.object({
  status: z.string(),
  k: z.number(),
  mode: z.string(),
  bridge: z.string(),
  base_hash: z.string(),
  selected_label: z.string().nullable().optional(),
  selection_reason: z.string().optional(),
  result_artifact_id: z.string().optional(),
  proposals: z.array(
    z.object({
      label: z.string(),
      before_hash: z.string(),
      after_hash: z.string(),
      changed: z.boolean(),
      notes: z.string(),
      diff_artifact_id: z.string(),
      model_call_id: z.string(),
    }),
  ),
  stages: z.array(
    z.object({
      label: z.string(),
      dim: z.string(),
      fell_back: z.boolean(),
      kept_base: z.boolean(),
      attempts: z.number(),
      notes: z.string(),
      error: z.string(),
    }),
  ),
});
export const logIndexSchema = z.object({
  schema_version: z.literal("1.0"),
  sha256: z.string(),
  total_lines: z.number(),
  size_bytes: z.number(),
  sections_truncated: z.boolean(),
  sections: z.array(
    z.object({
      kind: z.string(),
      priority: z.number(),
      line_start: z.number(),
      line_end: z.number(),
      marker_line: z.number(),
      step: z.string().nullable(),
      timestamp: z.string().nullable(),
    }),
  ),
});
export const logWindowSchema = z.object({
  artifact_id: z.string(),
  sha256: z.string(),
  total_lines: z.number(),
  lines: z.array(
    z.object({
      number: z.number(),
      content: z.string(),
      column: z.number(),
      complete: z.boolean(),
    }),
  ),
  next_line: z.number().nullable(),
  next_column: z.number().nullable(),
});
export const evidenceSchema = z.object({
  id: z.string(),
  kind: z.string(),
  artifact_id: z.string(),
  path: z.string().nullable(),
  line_start: z.number(),
  line_end: z.number(),
  excerpt: z.string(),
  excerpt_truncated: z.boolean().optional(),
  log_index: logIndexSchema.optional(),
  log_context_artifact_ids: z.array(z.string()).optional(),
  log_origin: z
    .object({
      source: z.enum(["network", "cache"]),
      cached_at: z.number().nullable(),
      expires_at: z.number().nullable(),
      artifact_id: z.string().optional(),
    })
    .optional(),
});
export const experienceVersionSchema = z.object({
  id: z.string(),
  domain: z.string(),
  version: z.number().int(),
  source_task_id: z.string(),
  status: z.enum(["active", "revoked"]),
  created_at: z.number(),
  content_hash: z.string(),
  cue: z.string(),
  lesson: z.string(),
  evidence_ids: z.array(z.string()),
  scope: z.literal("development_only"),
});
export const experiencePageSchema = z.object({
  items: z.array(experienceVersionSchema),
  total: z.number(),
  page: z.number(),
  page_size: z.number(),
});
export const experienceDetailSchema = experienceVersionSchema.extend({
  content: z
    .object({
      tree: z.record(z.string()),
      edits: z.string(),
      harness_hash: z.string(),
      profile_hash: z.string(),
      harness2_sha: z.string(),
      patch_hash: z.string(),
      candidate_id: z.string(),
      validation_id: z.string(),
      model_call_id: z.string(),
      input_snapshot_id: z.string(),
      input_snapshot_hash: z.string(),
      parent_version_ids: z.array(z.string()),
      model: z.string(),
      response_model: z.string().nullable(),
      case_id: z.string(),
    })
    .passthrough(),
});
export const experienceSchema = z.object({
  enabled: z.literal(true),
  snapshot_id: z.string(),
  snapshot_hash: z.string(),
  domain: z.string(),
  head_version: z.number(),
  entries: z.array(experienceVersionSchema),
  reflection_status: z.string(),
  reason: z.string().optional(),
  published_version_id: z.string().nullable(),
  draft: z
    .object({
      artifact_id: z.string(),
      cue: z.string(),
      lesson: z.string(),
      evidence_ids: z.array(z.string()),
      model_call_id: z.string(),
    })
    .nullable(),
});
export const taskSchema = z
  .object({
    id: z.string(),
    title: z.string(),
    repo: z.string(),
    case_id: z.string().nullable(),
    source_type: z.string(),
    head_sha: z.string().nullable(),
    snapshot_sha256: z.string().nullable().optional(),
    run_id: z.number().nullable(),
    run_attempt: z.number().nullable(),
    job_id: z.number().nullable(),
    profile_version: z.string().nullable(),
    runner_profile: z
      .object({
        profile_id: z.string(),
        profile_hash: z.string(),
        repository: z.string(),
        head_sha: z.string(),
        snapshot_sha256: z.string(),
      })
      .nullable()
      .optional(),
    source_snapshot: z
      .object({
        profile_id: z.string(),
        profile_hash: z.string(),
        repository: z.string(),
        head_sha: z.string(),
        snapshot_sha256: z.string(),
        archive_sha256: z.string(),
        manifest_artifact_id: z.string(),
        profile_artifact_id: z.string().optional(),
      })
      .nullable()
      .optional(),
    execution_label: z.string(),
    mode: z.enum(["standard", "adaptive"]),
    agent_tools: z.boolean().optional(),
    experiment_binding: z
      .object({
        id: z.string(),
        protocol: z.string().optional(),
        arm: z.string().optional(),
      })
      .passthrough()
      .nullable()
      .optional(),
    lifecycle: lifecycleSchema,
    phase: phaseSchema,
    phase_history: z.array(phaseSchema).optional(),
    outcome: z.string().nullable(),
    message: z.string(),
    state_version: z.number(),
    last_event_seq: z.number(),
    created_at: z.number(),
    started_at: z.number().optional(),
    finished_at: z.number().optional(),
    parent_task_id: z.string().nullable(),
    validation_status: z.string(),
    reproduction_status: z.string(),
    usage: z.object({
      calls: z.number(),
      tokens: z.number(),
      cost_usd: z.number().nullable(),
      usage_known: z.boolean(),
      model: z.string().optional(),
      provider: z.string().optional(),
      response_model: z.string().optional(),
    }),
    limits: z.object({
      max_wall_seconds: z.number(),
      max_model_calls: z.number(),
      max_tokens: z.number(),
    }),
    evidence: z.array(evidenceSchema),
    candidates: z.array(candidateSchema),
    validations: z.array(validationSchema),
    executions: z.array(executionSchema),
    model_calls: z.array(modelCallSchema).optional(),
    agent_actions: z
      .array(
        z.object({
          id: z.string(),
          name: z.string(),
          status: z.string(),
          model_call_id: z.string(),
          rollout_id: z.string(),
          round: z.number(),
          started_at: z.number(),
          finished_at: z.number().optional(),
          request_artifact_id: z.string(),
          result_artifact_id: z.string().optional(),
          error_code: z.string().nullable().optional(),
        }),
      )
      .optional(),
    harness2_sha: z.string().nullable().optional(),
    harness: harnessSchema.nullable().optional(),
    experience: experienceSchema.nullable().optional(),
    baseline: z.array(executionSchema).optional(),
    rollouts: z.array(
      z.object({
        id: z.string(),
        label: z.string(),
        status: z.string(),
        harness_hash: z.string(),
        harness_artifact_id: z.string().optional(),
        error: z.string().optional(),
      }),
    ),
    diagnosis: z
      .object({
        summary: z.string(),
        failure_category: z.string(),
        root_cause: z.object({
          description: z.string(),
          evidence_ids: z.array(z.string()),
          certainty: z.string(),
        }),
        limitations: z.array(z.string()),
        next_action: z.string(),
      })
      .nullable(),
    artifacts: z.array(
      z.object({
        id: z.string(),
        kind: z.string(),
        sha256: z.string(),
        size_bytes: z.number(),
        content_type: z.string(),
      }),
    ),
  })
  .passthrough();
export const caseSchema = z
  .object({
    id: z.string(),
    title: z.string(),
    description: z.string(),
    category: z.string(),
    repo: z.string(),
    head_sha: z.string().nullable(),
    snapshot_sha256: z.string().nullable(),
    profile_version: z.string(),
    execution_label: z.string(),
    blocked: z.boolean().optional(),
  })
  .passthrough();
export const healthSchema = z.object({
  scoring_ready: z.boolean().optional(),
  model_ready: z.boolean(),
  model_id: z.string().nullable(),
  runner: z
    .object({
      ready: z.boolean(),
      message: z.string(),
      backend: z.string().nullable(),
    })
    .passthrough(),
  adaptive_ready: z.boolean(),
  adaptive_message: z.string(),
  agent_tools_ready: z.boolean().optional(),
  max_model_calls: z.number().optional(),
});
export const modelCheckSchema = z.object({
  provider: z.string(),
  model_id: z.string(),
  status: z.literal("available"),
  checked_at: z.number(),
  message: z.string(),
});
export const previewSchema = z.object({
  repo: z.string(),
  run_id: z.number(),
  run_attempt: z.number(),
  head_sha: z.string(),
  message: z.string(),
  execution_supported: z.boolean(),
  jobs: z.array(
    z.object({
      id: z.number(),
      name: z.string(),
      conclusion: z.string(),
      execution_supported: z.boolean().optional(),
      profile_version: z.string().nullable().optional(),
      failed_steps: z.array(z.object({ number: z.number(), name: z.string() })),
    }),
  ),
});
export const taskListSchema = z.object({
  items: z.array(taskSchema),
  total: z.number(),
  page: z.number(),
  page_size: z.number(),
});
export type Task = z.infer<typeof taskSchema>;
export type Candidate = z.infer<typeof candidateSchema>;
export type Validation = z.infer<typeof validationSchema>;
export type Execution = z.infer<typeof executionSchema>;
export type Evidence = z.infer<typeof evidenceSchema>;
export type Case = z.infer<typeof caseSchema>;
