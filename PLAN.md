# MessageFilteringAgent Implementation Plan

## Goal

Build a self-contained Python application in this directory. It filters user-defined message types from manual input and QQ Mail, makes exactly one of three business decisions (`满足`, `不满足`, `不确定`), and routes matches or clarification requests through the selected output. WeChat input is not adopted in the current version; retained SIWX code is a future placeholder. Runtime code must never import from `Project/RagAgent`.

## Scope and Decisions

- Keep message type and filtering criteria user-configurable; recruitment is only an example.
- One selected JSON configuration file represents one Agent/profile and its isolated memory. On the first save of a new default `settings.json`, bind the profile name exactly once and rename the JSON file to `<profile_id>.json`; keeping the default name binds `default` and produces `default.json`. If no name is chosen then, it cannot be changed later. Existing JSON files from before this behavior are treated as already initialized and are not renamed for compatibility. Profile identity/path changes require a restart. `--config <path>` is the non-packaged startup entry point; separate processes may run different profiles concurrently, each with its own state and local UI port.
- The local browser UI edits the active config and manages that same profile's memory in one settings workspace. It does not silently switch the identity of a running Agent.
- While the page is open, poll lightweight activity summaries every 3 seconds; update profile-scoped pending/filtered badges automatically and refresh the currently visible list when its records change. When another tab is active, update counts only; opening the list loads current records.
- WeChat automatic input is not adopted in the current version. SIWX JSON parsing, local API, synchronization/export, cursor, and cleanup code are retained only as a future placeholder; do not activate it or claim live WeChat support. The Agent does not connect to the WeChat desktop client or decrypt its database.
- Startup recovery currently applies to QQ Mail. If SIWX is reconsidered later, validate its per-chat composite timestamp/ID cursor against real exports before adoption; add overlapping timestamp fallback only if evidence shows it is needed. Use IMAP UID/UIDVALIDITY checkpoints for mail, with a bounded rescan after mailbox identity reset. Checkpoints, not a clean-shutdown timestamp, are the primary recovery mechanism.
- QQ Mail body extraction prefers non-attachment `text/plain`; when absent, extract readable text and links from `text/html` without executing markup. Binary/image attachment OCR remains a separate low-priority item.
- Deduplicate transport IDs and canonicalized exact-content fingerprints across sources within one filtering profile. Retention defaults to 30 days and is configurable. Do not semantically merge near-duplicates in v1.
- Use LangGraph for the model workflow. Operational errors are retry/error states, not a fourth business decision. `满足` is automatically delivered; `不满足` is filtered; `不确定` pauses for local user input and resumes the same run.
- Manual input accepts up to 20 independent chat transcripts per submission, with one large input box per transcript; each transcript can contain multiple target items plus noise. A structured LLM preprocessing call splits each transcript into ordered fragments while preserving all content, including noise; each fragment then goes through the same deduplication, classification, and routing workflow as other sources. Obvious unrelated noise is classified `不满足` and archived, target matches are delivered, and uncertain target items create durable pending questions; show transcript and fragment indices, and continue after per-transcript or per-fragment errors.
- In formal workflows, archive messages finally marked `不满足` for a configurable retention period (default 30 days), scoped to the handling profile. Preserve the original message, message date, source/location, profile, and decision reason. Provide a “过滤信息” tab for that profile and an explicit action to promote one archived message into the existing pending-review queue; promotion removes the archive row immediately, is idempotent, and must not automatically deliver it. The promoted item must not recreate that same archive row regardless of its later review outcome.
- Every classification must include a concise, non-empty, user-visible reason tied to the active criteria and quoted message facts: satisfied identifies the matching condition and fact; not satisfied identifies the violated/excluded condition and fact; uncertain names the condition not explicitly described or still ambiguous. Do not invent evidence or provide hidden chain-of-thought.
- Interpret `or` criteria as alternatives, and judge at the granularity specified by the user. Do not require list items to pair attributes unless the criteria explicitly require the same entity to have those attributes. If that explicit relationship is missing, return uncertain; only explicit violations support not satisfied.
- QQ Mail is the first output. Keep QQ-message output as a conditional adapter until a workable and acceptable access method is verified. Other email providers should fit the provider adapter boundary.
- UI v1 is a local browser UI served on the PC and bound to `127.0.0.1`; no phone/remote access in v1. Keep UI/backend boundaries suitable for a future mobile client or cloud deployment.
- Request Windows `ES_SYSTEM_REQUIRED` while active; allow display-off. Do not promise protection against forced sleep, shutdown, crash, or power loss.
- No secrets in source control, plan, or progress notes. Use a secure local credential mechanism.
- Model API credentials are shared by profiles that use the same normalized model access URL, but isolated across different URLs; QQ mailbox authorization codes are scoped to the sender account. Legacy global model keys migrate only to the default URL. Actual Windows keyring behavior remains unverified per user decision.
- Organize code by responsibility, with structured sections and useful comments for intent, contracts, and non-obvious behavior.

## Phases

1. Create project metadata, local handoff notes, normalized message/decision models, settings, and persistent storage.
2. Implement message-ID and TTL content deduplication; test expiry, cross-source collisions, and profile isolation.
3. Implement source adapters behind interfaces: manual import and QQ Mail IMAP recovery. Keep the SIWX JSON importer as dormant future-placeholder code; do not wire it into supported current inputs. Never claim gap-free source recovery unless the adapter can establish it.
4. Implement the LangGraph classify/route/clarify/resume workflow and QQ Mail output. Keep QQ-message delivery gated on verifying its access method.
5. Implement localhost-only UI, persistent pending questions, and Windows keep-awake lifecycle.
6. Add tests, operating instructions, and interruption/restart validation. Record actual commands/results in `PROGRESS.md`.

Detailed local and live-account test instructions are maintained in `ACTUAL_TEST_GUIDE.md`; live QQ/WeChat steps remain user-controlled and are not considered verified until performed with test data.

## Verification Gates

- Run focused tests for models, configuration, deduplication/TTL, routing, recovery checkpoints, clarification/resume, and output provenance.
- Mock network/model/mail integrations by default. Use live accounts only with explicit test credentials and test sends.
- Verify IMAP UIDVALIDITY reset behavior. Revisit SIWX cursor/timestamp validation only if that future input method is explicitly adopted.
- Verify local UI binds only to localhost and credentials are not exposed.
- Per user decision, do not perform environment-specific Windows keyring read/write or `powercfg /requests` validation; document both as unverified instead of treating them as a remaining test gate.
- If GitHub-dependent work fails to connect, stop retries, record the exact operation/error, preserve local work, and wait for the user's resume instruction.
- A fresh session must read this file and `PROGRESS.md`, inspect current workspace changes, and continue from the last verified checkpoint without claiming unrecorded results.

## Unified Profile-scoped Configuration and Memory

Implementation direction: keep each config file bound to one Agent/profile and its memory; use the shared local SQLite database with profile-scoped records when configs share a data directory. Never rewrite the active criteria automatically.

Status: implemented and verified with offline tests. The user confirms real QQ Mail end-to-end validation is complete. Live model summarization remains unverified; WeChat input is not adopted, and the packaged launcher is tracked separately below.

Reference prerequisite completed: reviewed the local RagAgent memory example. The referenced GitHub page could not be fetched in that session, so the local example was used as required.

- `Settings.load(--config path)` retains the selected file when saving; configs without an explicit `profile_id` derive it from the filename. Concurrent profile processes use separate localhost ports.
- The default profile name is bound on the first settings save. A selected name renames `settings.json` to `<profile_id>.json`; leaving `default` unchanged creates `default.json`. The field becomes read-only after saving, profile/path changes request a restart, and the default launcher resumes the sole profile JSON when `settings.json` no longer exists. Existing JSON without a binding marker is treated as already initialized. Memory remains in SQLite rather than inside the JSON file; the newly bound profile's memory store is normally empty at initialization.
- Memories, pending questions, source checkpoints, message deduplication, and imported-file fingerprints are scoped by profile so different Agents do not share state.
- The review UI provides `满足`, `不满足`, `满足并说明理由`, and `不满足并说明理由`. Every explicit choice settles the current item without changing the active rule or reclassifying it. A supplied reason is summarized into an editable profile memory for future classification only; after the decision is saved, the item is removed from the pending UI immediately.
- Memory remains separate from active criteria. It supplements classification but cannot override conflicting criteria or rewrite them.
- The local browser workspace edits active configuration and supports memory list, add, edit, enable/disable, and delete.
- Duplicate-file filtering is consolidated into the memory module. The retained SIWX placeholder has regression coverage for renamed identical fixture files; caught processing errors and orderly shutdown release claims immediately. A hard process termination leaves a claim lease that expires after one hour. This remains distinct from message-ID and TTL-based message deduplication.
- Offline tests cover config persistence, profile isolation, memory CRUD/context, all four review choices, legacy-state migration, retained SIWX fixture behavior, QQ Mail-to-SIWX deduplication logic, cursor advancement, and orderly interruption. SIWX tests do not indicate current user-facing support.
- The current UI/API does not allow enabling or changing to a WeChat input mode. Existing SIWX or retired wxauto settings fall back to manual mode on load; retained SIWX code is dormant future-placeholder code.

## Portable Launcher and Packaging

Implementation status: launcher and portable ZIP workflow are implemented and verified. The selected format is a Windows PyInstaller onedir ZIP; the startup configuration UI reuses the local web app styling. Profile selection/creation/deletion runs before an Agent starts, profile-scoped SQLite data is deleted after one confirmation, credentials are scoped by model URL/mailbox, and create failures roll back only the new config and changed credentials. Existing CLI first-save/profile naming behavior remains supported. On 2026-10-06, all 98 offline tests passed; the final 621-entry ZIP passed CRC validation, its EXE matches `build/portable`, and all six packaged static assets match the workspace sources. The packaged EXE `--help` smoke test exited 0 without starting an Agent. Browser UI smoke used fake API responses only. Windows keyring and keep-awake environment validation remain excluded per user decision.

The launcher now shuts down after 30 minutes without user activity; interaction heartbeats refresh the timer, and already-started Agent processes continue independently. Closing an Agent also closes its page when permitted, or navigates the tab to a blank page. Offline tests cover heartbeat and idle shutdown.

## Deferred or Conditional

- Low priority; do not implement unless the user explicitly requests it: support image-based message understanding, considering OCR and/or vision models. Before implementation, define which inputs are in scope (manual images, SIWX exports, and/or mail attachments), plus privacy and cost boundaries. Mention this item whenever reporting the project backlog to the user.
- QQ-message output pending a verified acceptable integration method.
- Phone access and cloud deployment are future extensions, not v1.
- Copilot quota cannot be monitored directly. Pause only when a visible warning or user-provided usage indicates 80% or more.