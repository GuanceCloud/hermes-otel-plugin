# LLM Error Sessions Diagnosis

## Scope

- Time range from screenshot: `2026-06-07 15:57:00 +08:00` to `2026-06-08 15:57:00 +08:00`
- Focused trace IDs from screenshot:
  - `d3ae95d3e620e5513c1e1887f40dd1ff`
  - `4f5299a566afca4733ebfa75916275fe`
  - `d00c5e13711943761103f5f34fa6ba7e`
- Focused session IDs from screenshot:
  - `20260608_154219_1f6de9`
  - `20260608_153033_d77051`
  - `20260608_152602_fe3106`

## Query Facts

- `owl.data.query` and Guance MCP `owl.data.simple_query` against `T` with the screenshot trace IDs returned empty results in the available workspace context.
- `owl.data.query` against `T` and `LLM` with the screenshot session IDs also returned empty results.
- A broad `T::*:(count(*)) [1d] BY service, resource, status` query did return tracing data, so query execution itself was functional.
- Therefore, this report cannot confirm the three screenshot traces from backend query data. The runtime conclusion below is based on visible UI facts plus repository code paths.

## Visible UI Facts

- The three highlighted rows all show `final_status = finalized`.
- The three highlighted rows all show input tokens `0`, output tokens `0`, and total tokens `0`.
- Durations are short to medium: about `5.40 s`, `4.65 s`, and `24.22 s`.
- This pattern is consistent with a request span being opened before model completion, then closed by final session cleanup without receiving model usage or response details.

## Confirmed Local Error Evidence

- User screenshot and local Hermes logs confirm these sessions failed with `AuthenticationError [HTTP 401]`.
- Error code: `token_invalidated`.
- Provider/model: `openai-codex` / `gpt-5.5`.
- Endpoint: `https://chatgpt.com/backend-api/codex`.
- Error message: `Your authentication token has been invalidated. Please try signing in again.`
- Matching local log entries:
  - `2026-06-08 15:26:19.845 +08:00` for `20260608_152602_fe3106`
  - `2026-06-08 15:31:14.352 +08:00` for `20260608_153033_d77051`
  - `2026-06-08 15:42:29.280 +08:00` for `20260608_154219_1f6de9`

## Code Findings

- `src/plugin.py` maps `on_session_finalize` to `TraceManager.finalize_session(..., outcome="finalized")`.
- `src/trace_manager.py` creates an `llm` span in `start_api_request`.
- `finish_api_request` only records `finish_reason`, response model, usage, and token fields when the `post_api_request` hook runs.
- Hermes provides an `api_request_error` hook for failed provider requests, but the plugin version used during the incident did not register it.
- If a model request raises before `post_api_request` and the plugin does not handle `api_request_error`, the active `llm` span stays in `turn.active_requests`.
- During final cleanup, `_finalize_turn_state` closes active requests with span status `ERROR` and description `orphaned_request`, but without the failure hook it has no provider exception message to attach.
- The parent `hermes_request` and `agent_run` spans receive `final_status = finalized` and token totals from aggregate counters. If `finish_api_request` never ran, those totals remain `0`.

## Judgment

- Fact: The plugin version used during the incident did not handle Hermes `api_request_error`, so failed LLM request details were not exported.
- Fact: The three sessions failed because the Codex OAuth token was invalidated and the provider returned HTTP 401.
- Inference: The failure happened during or before the LLM response completion path, so `post_api_request` did not provide usage or error details. Because `api_request_error` was not registered, final cleanup then produced `finalized` parents and an orphaned/error child span, but the actual model/provider error reason was not exported.
- Confidence: High for the runtime failure cause and high for the missing plugin hook mechanism.

## Next Steps

1. Register and handle `api_request_error` in `hermes-otel-plugin`.
2. Write `error_type`, `error_message`, `error_code`, `http_status_code`, `retry_count`, `max_retries`, `retryable`, and `error_reason` to the `llm` span.
3. For terminal API errors, also copy the same error summary to `hermes_request` and `agent_run` so list/detail pages can surface the cause without requiring child-span inspection.
4. Mark `finalized` sessions with terminal API errors as `failed`.

## Fix Status

- Implemented in this working tree after diagnosis.
- Added `api_request_error` to `plugin.yaml` and plugin registration.
- Added `TraceManager.record_api_request_error`.
- Added tests for the registration path and 401/token-invalidated span behavior.
