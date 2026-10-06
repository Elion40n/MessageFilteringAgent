# Implementation Progress

## Checkpoint

- Status: profile-scoped config, unified memory, and portable launcher implementation complete; SIWX input is parked as a disabled future placeholder; final ZIP and extracted EXE verified.
- Current phase: offline implementation checks complete; user confirms real QQ Mail end-to-end validation is complete. Current supported inputs do not include WeChat.
- Last verified action: full `.venv\Scripts\python.exe -m unittest discover -s tests -q` passed 96 tests. `dist/MessageFilteringAgent-portable.zip` rebuilt with 621 files, passed CRC and extraction, and its frozen EXE `--run-agent --help` smoke exited 0; browser mock verified launcher form serialization without saving credentials.
- Launcher deletion requirement: one explicit confirmation deletes the selected profile config and all associated profile-scoped SQLite data; shared credentials remain when other profiles use them.
- Packaging decision: PyInstaller onedir distributed as a portable ZIP; launcher/setup uses current web UI styling; settings, SQLite, and credentials stay outside the extracted app directory.
- Local Git state: the workspace was previously not inside a Git repository; a local `main` branch has since been initialized. Preserve all existing project changes.
- Confirmed the approved requirements and constraints are captured in `PLAN.md`.
- 2026-10-06: Expanded `README.md` with the project reference/Copilot attribution, non-commercial notice, Bilibili link placeholder, roles of the three other project Markdown files, and an AI-first-reading recommendation. Workspace text search confirmed the added content and all three linked documents exist. No code tests were needed for this documentation-only change; two preliminary Python checks were blocked by validation-script regex/PowerShell encoding issues.
- 2026-10-06: Reordered the README startup instructions so the profile launcher is the recommended primary method after installation; direct Agent CLI commands are now under development/diagnostics. Workspace text search confirmed command paths and section ordering.
- 2026-10-06: Updated README for distribution of a GitHub-provided prebuilt portable ZIP: ordinary users no longer need Python or to build the launcher; source setup and packaging steps are labeled for developers, with the release URL left as a placeholder. Workspace search confirmed the user/developer instructions and artifact path.
- 2026-10-06: Documented that the prebuilt ZIP is included at `dist/MessageFilteringAgent-portable.zip` for the planned full-project GitHub upload. Updated `.gitignore` to include only that ZIP while continuing to ignore other `dist` outputs; verified the ZIP exists (31,767,351 bytes) and the README/ignore entries are present.
- 2026-10-06: Fixed manual transcript submission so all input textareas clear after a successful response while remaining intact on request failure. `.venv\Scripts\python.exe -m unittest discover -s tests -p test_ui.py -v` passed (14 tests); `app.js` diagnostics reported no errors. The test suite has no browser-level assertion for this JavaScript interaction.
- 2026-10-06: Added a confirmed “关闭 Agent” control to the Agent top bar, reusing the local-origin-checked graceful shutdown endpoint; mobile layout keeps the control visible. Extended static UI assertions. `.venv\Scripts\python.exe -m unittest discover -s tests -p test_ui.py -v` passed (14 tests); diagnostics for HTML, JS, CSS, and the test file reported no errors.
- 2026-10-06: Rebuilt and replaced `dist/MessageFilteringAgent-portable.zip` from the current source using a separate staging directory because two live Agents (ports 8764 and 8767) locked the existing `build/portable` executable. The final ZIP has 621 entries, passed `ZipFile.testzip()`, and its `app.js`, `index.html`, and `style.css` match the workspace sources byte-for-byte. Size: 31,766,903 bytes; SHA-256: `b570f3b6ed19c14f7e1b3488d5c12c6e596102d92ef0726b4d7d009dbb8981ae`. The active Agents were not stopped; the old `build/portable` directory therefore remains in use and was not replaced.
- 2026-10-06: At the user's request, closed all running Agent and launcher processes before the final changes. Added page closure/fallback after Agent shutdown and 30-minute launcher idle shutdown with interaction heartbeats; an idle launcher does not stop already-started Agents. Full `.venv\Scripts\python.exe -m unittest discover -s tests -q` passed 98 tests. Rebuilt `build/portable` and replaced `dist/MessageFilteringAgent-portable.zip` using `scripts/build_portable.py`; the ZIP has 621 entries and passes CRC, includes the same EXE as `build/portable`, and all six static assets match the source. Packaged EXE `--help` exited 0 without starting a server. ZIP size: 31,768,488 bytes; SHA-256: `571bce465c6c8c63fdd2fe1da6ca2a119fb5139677108689e3377728d71015b1`. README, ACTUAL_TEST_GUIDE, and PLAN were synchronized.
- 2026-10-06: Added `LICENSE` for PolyForm Noncommercial 1.0.0 per the user's choice and changed the README declaration to “非商用源码项目” with a license link. Verified the principal license sections and README link are present; README and LICENSE diagnostics report no errors. No code tests were needed for this documentation/license change.
- 2026-10-06: Added `.github/workflows/ci.yml` for Windows/Python 3.11 CI on push, pull requests, and manual dispatch with `contents: read` permission. Local `.venv\Scripts\python.exe -m unittest discover -s tests -v` exited 0 (98 tests); YAML parsed successfully and workflow diagnostics reported no errors. Configure branch protection to require check `CI / Tests (Python 3.11)` after the workflow has run on GitHub.
- 2026-10-06: Initialized local `main`, added `origin` for `https://github.com/Elion40n/MessageFilteringAgent.git`, and committed 42 files (including the portable ZIP; excluding `.venv` and `build`). GitHub browser/device auth is now valid as `Elion40n`; the public repository was created. `git push -u origin main` failed with HTTPS connection reset/timeout, and GitHub API confirms remote `main` is still absent. Retry push when GitHub Smart HTTP connectivity recovers; do not assume upload completed.
- 2026-10-06: After the initial GitHub CI run, fixed both Windows 8.3 temp-path string assertions in `test_launcher.py` by comparing resolved paths for `data_dir` and the `--config` argument. `test_launcher.py` passes 13 tests; the full suite is being rerun before updating PR #1.
- 2026-10-06: PR #1 was merged after its Windows/Python 3.11 CI passed; the post-merge `main` CI also passed. Pre-release VirusTotal checks found 4/64 ZIP and 7/71 EXE detections (including Microsoft Wacatac, Elastic high-confidence malicious, and Skyhigh backdoor labels), while Windows Defender found no matching threat. The EXE is unsigned. Per the user's request, README now discloses this risk and source-build fallback; do not create a Release until the alert is investigated or the user confirms the risk-based release decision.
- 2026-10-06: Consolidated contribution and security-report guidance into a short README section instead of adding separate `CONTRIBUTING.md`/`SECURITY.md` files. The private vulnerability-reporting channel remains to be enabled and filled in after repository creation.
- 2026-10-06: Replaced the README Bilibili placeholder with the supplied demo link, added the user's contact email, and made it the private security-reporting channel.
- README and `.gitignore` are present.
- Added `ACTUAL_TEST_GUIDE.md` covering safe manual, restart, mailbox, and SIWX test procedures; linked it from README and PLAN.
- Historical documentation update: revised `ACTUAL_TEST_GUIDE.md` for `--config` profiles and concurrent agents while the launcher was still deferred; launcher status is tracked in the current checkpoint above.
- Implemented normalized message/decision models, validated settings, SQLite deduplication/checkpoints, retry release, and persisted clarification/resume.
- Implemented LangGraph three-way classification/routing, OpenAI-compatible classifier, OS credential-store interface, SMTP delivery formatting, local browser UI/API, and Windows keep-awake lifecycle code.
- Implemented IMAP UID/UIDVALIDITY reader. SIWX JSON reading and cursors remain in source as future-placeholder code; neither SIWX nor wxauto is a currently supported live WeChat input.
- Fixed QQ Mail HTML-only body loss: parser now prefers non-attachment plain text and falls back to readable HTML text/link extraction while ignoring script/style. A user-authorized read-only check of the first isolated `MFA_TEST` message confirmed the previous parser returned 0 body characters and the updated parser extracts 665 characters; no mail was marked read or sent.
- Added per-config CLI startup with selected-file persistence, config-derived profile IDs, and free-port selection for concurrent local agents.
- Scoped message deduplication, pending questions, source checkpoints, and memory to the profile; legacy pending/checkpoint records migrate to the default profile.
- Fixed QQ Mail checkpoint reads to use the active profile, so non-default Agents resume from their own saved UID.
- Added and passed a cross-source regression test: a previously processed QQ Mail message is skipped by SIWX through the shared content fingerprint, while the SIWX cursor advances.
- Added `MemoryStore` for feedback memories and SHA-256 file claims; SIWX import filters renamed identical files and releases claims after processing failures.
- Added direct/reasoned user review choices, LLM memory summarization, memory-aware reclassification, and a profile settings page with memory CRUD.
- Added visible per-question processing status for direct/reasoned human judgments; reasoned choices report summarization/reclassification in progress, and failures restore retry with an error message.
- Moved profile memory management from the settings panel to its own “记忆” tab; opening the tab refreshes the current profile's memory list.
- Updated manual input to accept up to 20 independent chat transcripts in addable text boxes; each record is split into ordered fragments, preserves noise for classification, and routes each fragment through the formal workflow with group-aware results and isolated per-record/per-fragment errors.
- Made classifier explanations required and nonblank; strengthened the prompt to map each decision to explicit criteria and message evidence, and labeled the reason in manual results/email output.
- Clarified domain-neutral message-level criterion semantics in the classifier prompt: OR alternatives, match at the user's stated granularity, never add an unstated entity/attribute relationship, require same-entity pairing only when explicitly requested, use uncertainty for explicitly required but missing relationships, and use not-satisfied only for explicit violations. Real model output behavior remains unverified.
- Added a profile-scoped “过滤信息” archive for formal not-satisfied outcomes, with configurable 30-day default retention, source/message dates and reason; promotion deletes the archive row and creates a linked pending-review item, remains idempotent, and never restores that original archive row regardless of review outcome. Manual messages now use this same formal route.
- Added a profile-scoped `/api/activity` summary and 3-second browser polling; pending/filtered counters update globally, while the currently open review/archive list refreshes only when its activity signature changes.
- Fixed human-review feedback handling: show processing state immediately, remove terminally handled cards immediately, and let activity polling reconcile the list after a successful submission.
- Human review now requires an explicit final choice; supplied reasons are summarized only into profile memories for future use. Removed the dead free-form reclassification branch, `needs_input` response handling, follow-up button, and unused pending-decision update method; successful submission removes the card immediately and the endpoint returns only a saved acknowledgment.
- Per the user's direction, removed the wxauto listener, configuration/UI fields, tests, and optional dependency; the project `.venv` package was uninstalled. Legacy wxauto/SIWX modes now fall back to manual; SIWX code is retained but is not a supported live input.
- Previously implemented optional loopback-only SIWX API synchronization/export/import and native JSON parsing; these code paths are now retained but disabled as a future WeChat input placeholder. No real SIWX service or WeChat client was used in tests.
- The retained SIWX placeholder discovers exports recursively under its configured root; its fixture coverage does not make the path active or supported.
- Automatic SIWX export cleanup implementation and its path-safety checks are retained for future use; the current settings UI/API cannot activate them, and loading an older SIWX config falls back to manual mode.
- Marked SIWX code as a future placeholder in adapter/config/runner comments and user docs; disabled the UI option, blocked enabling it through runtime settings, and added legacy-config fallback tests.
- Historical validation at the SIWX-placeholder checkpoint passed 68 tests; SIWX HTTP interactions were mocked, and no real SIWX service or WeChat account was connected. See the checkpoint above for the latest 79-test result.
- Bound the default profile name on first save, renamed its JSON file to `<profile_id>.json`, locked the field afterward, and made profile/path changes trigger a restart; the default fallback is `default.json`. SQLite profile state is safely moved if present, while a new profile's memory store remains empty.
- Removed the compatibility path that treated pre-feature JSON files without `profile_name_bound` as uninitialized; those files are now considered already initialized. Deleted the sole unused `settings.json` from `%LOCALAPPDATA%\MessageFilteringAgent-Test` at the user's request; left `agent.sqlite3` untouched.
- At the user's request, deleted `%LOCALAPPDATA%\MessageFilteringAgent-Test\recruitment.json` and its one pending question for profile `recruitment`; verified the config is absent and pending count is zero. Preserved `agent.sqlite3` and all other profile state.
- Legacy SIWX and wxauto config modes now fall back to manual; current UI/API does not expose or accept live WeChat input.
- Orderly shutdown during SIWX file processing immediately releases its file claim; a regression test verifies the interrupted file can be claimed again.
- Clarified `ACTUAL_TEST_GUIDE.md`: QQ Mail resumes available unprocessed messages; pending questions wait for a human; manual input does not backfill downtime; SIWX is documented as a future placeholder only.
- Added Chinese documentation/comments alongside retained English text across Python modules, source loops/error paths, workflow/storage contracts, and frontend JS security/state flows.
- Started the app from the source tree on `127.0.0.1:8766` with a dedicated temporary data directory; the browser loaded the single transcript input and verified mocked split/result rendering. The server remains available for manual inspection.

## In Progress

- Follow `ACTUAL_TEST_GUIDE.md` with user-controlled test accounts/samples; do not connect to real QQ/WeChat accounts without explicit test setup.
- Reviewed the local RagAgent memory example as the required prerequisite before implementing the planned reasoned-feedback enhancement. The external GitHub page for the referenced repository could not be fetched successfully in this session, so the local example was used as the fallback reference source.
- The LLM summarization path has not been tested against a live model endpoint. User confirms QQ Mail end-to-end validation separately; WeChat integration remains unsupported.

## Remaining

- Low priority, user-authorized only: support image-based message understanding via OCR and/or vision models. Define supported input sources, privacy, and cost before implementation. Do not implement without an explicit request; mention it when the user asks to review the backlog.
- No WeChat account/client simulation; WeChat input remains unsupported.
- No launcher-specific implementation remains. Consider additional manual review of packaged antivirus reputation and live user-controlled profile creation when the user is ready; no real credentials or Agent profile were created in this implementation session.
- By user decision, do not perform Windows keyring backend read/write or actual keep-awake environment validation. Both remain explicitly unverified on the target machine.

## Validation

- `python --version` -> `Python 3.11.4`.
- First test run found SQLite connections remained open on Windows because connection context managers do not close handles; replaced this with an explicit commit/rollback/close context manager.
- `python -m unittest discover -s tests -v` -> 25 tests passed. IMAP/SIWX tests use fake IMAP responses and fixture JSON only; these do not prove compatibility with live QQ Mail or a real SIWX export.
- `python -m unittest discover -s tests -q` after bilingual comment pass -> 25 tests passed.
- `python -m compileall -q message_filtering_agent tests` -> passed.
- Workspace diagnostics -> no errors.
- `.venv` `python -m unittest discover -s tests -q` -> 25 tests passed.
- `.venv\Scripts\python.exe -m unittest discover -s tests -q` after profile/memory, QQ checkpoint, wxauto-to-SIWX dedup, and automatic shutdown updates -> 42 tests passed.
- User-provided `.venv\Scripts\python.exe -m unittest discover -s tests -v` on 2026-10-04 -> 42 tests passed in 3.723 seconds. UI HTTP log entries include the expected 403 for a foreign origin and successful local API requests; all source/model integrations remain mocked or offline.
- `.venv\Scripts\python.exe -m unittest discover -s tests -p test_ui.py -v` after adding the memory tab -> 5 UI tests passed in 2.941 seconds, including static tab/panel assertions and memory CRUD API behavior.
- `.venv\Scripts\python.exe -m unittest discover -s tests -q` after adding the memory tab -> 42 tests passed in 5.919 seconds.
- `.venv\Scripts\python.exe -m unittest discover -s tests -p test_core.py -v` after first-save profile naming -> 20 tests passed, including empty-memory naming, default fallback, JSON rename, and rejection of later renaming.
- `.venv\Scripts\python.exe -m unittest discover -s tests -p test_ui.py -v` after profile-name UI wiring -> 5 tests passed, including response filename, bound state, and restart-required status.
- `.venv\Scripts\python.exe -m unittest discover -s tests -v` after profile-name initialization changes -> 44 tests passed in 4.009 seconds.
- `.venv\Scripts\python.exe -m unittest discover -s tests -p test_core.py -v` after removing legacy config renaming support -> 21 tests passed in 0.569 seconds.
- `.venv\Scripts\python.exe -m unittest discover -s tests -q` after removing legacy config renaming support -> 45 tests passed in 3.892 seconds.
- `.venv\Scripts\python.exe -m unittest discover -s tests -p test_ui.py -v` after adding the isolated output panel -> 6 tests passed in 3.233 seconds, including assertions that test classification does not call delivery or persist workflow state.
- `.venv\Scripts\python.exe -m unittest discover -s tests -q` after adding the isolated output panel -> 46 tests passed in 4.478 seconds.
- Historical UI check after splitting the manual input and processing-results tabs -> 6 tests passed.
- `.venv\Scripts\python.exe -m unittest discover -s tests -q` after requiring and displaying classification reasons -> 47 tests passed in 4.852 seconds.
- `.venv\Scripts\python.exe -m unittest discover -s tests -p test_core.py -v` after clarifying domain-neutral message-level matching rules -> 23 tests passed.
- `.venv\Scripts\python.exe -m unittest discover -s tests -q` after generalizing message-level matching rules and forbidding unstated relationships -> 48 tests passed in 4.276 seconds.
- `.venv\Scripts\python.exe -m unittest discover -s tests -p test_core.py -v` after adding filtered archive storage -> 26 tests passed, including expiry, profile isolation, promotion and Runtime wiring.
- `.venv\Scripts\python.exe -m unittest discover -s tests -p test_ui.py -v` after adding the “过滤信息” UI/API -> 7 tests passed.
- `.venv\Scripts\python.exe -m unittest discover -s tests -q` after filtered archive and promotion implementation -> 52 tests passed in 5.314 seconds.
- `.venv\Scripts\python.exe -m unittest discover -s tests -p test_core.py -q` after archive reprocessing behavior -> 27 tests passed.
- `.venv\Scripts\python.exe -m unittest discover -s tests -q` after completing filtered archive lifecycle -> 53 tests passed in 5.342 seconds.
- `.venv\Scripts\python.exe -m compileall -q message_filtering_agent tests` -> passed after implementation.
- Workspace diagnostics after implementation -> no errors.
- `.venv\Scripts\python.exe -m message_filtering_agent.app --help` -> exit 0; `--config` option is available.
- Started an isolated temporary-data UI on `127.0.0.1:8767`; health, settings, and memory-list endpoints returned HTTP 200. No existing profile data or accounts were used.
- `.venv\Scripts\message-filtering-agent.exe --help` -> exit 0; installed CLI entry point is available.
- `node --check message_filtering_agent/static/app.js` could not run because `node` is not installed/available in the current terminal.
- Historical environment note: `powercfg /requests` required elevation and was not run; no elevation was attempted. Per the user's later decision, do not retry this check.
- User-authorized read-only QQ IMAP `LIST` against the configured mailbox returned the nested test mailbox as `&UXZO1mWHTvZZOQ-/MFA_TEST`; no folder was selected and no mail was fetched or sent. The test guide now requires using the full server-returned IMAP name rather than only the QQ web UI leaf name.
- Runtime import/instantiation/settings update smoke check passed.
- Historical dependency probe: `langgraph` and `langchain_openai` were importable per `find_spec`; FastAPI and keyring were not detected, while uvicorn was installed. A later user-provided `.venv\Scripts\python.exe -m pip show keyring langgraph langchain-openai` reported keyring 25.7.0, langgraph 1.2.12, and langchain-openai 1.6.7 installed in the project virtual environment. Package presence does not verify Windows credential-backend read/write behavior. At that earlier checkpoint no external account integration had been attempted; the user later confirmed QQ Mail end-to-end validation complete.
- Read-only `pip show` later confirmed the earlier isolated editable installation completed in `.venv`; no global Python installation was changed and no second installation was run.
- User confirms real QQ Mail end-to-end validation is complete. Automated source tests still use fixtures/fakes. WeChat end-to-end validation has not been run because that input path is not adopted.
- User reported that a test email was received on 2026-10-04. The route (arrival in the monitored IMAP folder versus delivery to the SMTP recipient) was not specified, so this records receipt only and does not claim end-to-end processing, cursor commit, or SMTP delivery.
- `.venv\Scripts\python.exe -m unittest discover -s tests -p test_sources.py -v` after adding HTML-only email parsing -> 11 tests passed.
- `.venv\Scripts\python.exe -m unittest discover -s tests -q` after fixing HTML-only email parsing -> 55 tests passed in 5.503 seconds.
- `.venv\Scripts\python.exe -m unittest discover -s tests -p test_ui.py -q` after adding human-review processing feedback -> 7 tests passed in 3.849 seconds.
- `.venv\Scripts\python.exe -m unittest discover -s tests -p test_ui.py -q` after adding activity refresh -> 8 UI tests passed.
- `.venv\Scripts\python.exe -m unittest discover -s tests -q` after adding activity refresh -> 56 tests passed in 6.918 seconds.
- `.venv\Scripts\python.exe -m unittest discover -s tests -p test_core.py -q` after deleting promoted archive rows and adding legacy migration coverage -> 28 tests passed.
- `.venv\Scripts\python.exe -m unittest discover -s tests -p test_workflow.py -v` after suppressing re-archive of promoted items -> 11 tests passed.
- `.venv\Scripts\python.exe -m unittest discover -s tests -p test_ui.py -q` after asserting promoted rows disappear -> 8 tests passed.
- `.venv\Scripts\python.exe -m unittest discover -s tests -q` after final archive lifecycle behavior -> 58 tests passed in 6.775 seconds.
- `.venv\Scripts\python.exe -m unittest discover -s tests -p test_ui.py -q` after separating review processing/refresh feedback -> 8 tests passed in 5.344 seconds.
- `.venv\Scripts\python.exe -m unittest discover -s tests -q` after separating review submission and list-refresh errors -> 56 tests passed in 7.795 seconds.
- `.venv\Scripts\python.exe -m unittest discover -s tests -q` after removing legacy promoted-row migration and UI handling -> 57 tests passed in 6.486 seconds.
- `.venv\Scripts\python.exe -m unittest discover -s tests -p test_workflow.py -q` after making reasoned human choices final -> 12 tests passed.
- `.venv\Scripts\python.exe -m unittest discover -s tests -q` after making human choices final and storing reasons only as memories -> 58 tests passed in 8.949 seconds.
- `.venv\Scripts\python.exe -m unittest discover -s tests -p test_ui.py -q` after removing the pending result payload -> 9 tests passed.
- `.venv\Scripts\python.exe -m unittest discover -s tests -q` after removing the obsolete pending reclassification path -> 59 tests passed in 8.518 seconds.
- `.venv\Scripts\python.exe -m unittest discover -s tests -p test_sources.py -q` after adapting the live listener to wxauto4 -> 12 tests passed.
- `.venv\Scripts\python.exe -m unittest discover -s tests -q` after wxauto4 integration and optional dependency documentation -> 60 tests passed.

## Next Action

- User confirms real QQ Mail end-to-end validation is complete. Keep automated-test/mock coverage distinct from that live validation; WeChat input remains unsupported. Per user decision, Windows keyring and keep-awake environment behavior remain unverified and are not planned for validation.

## Resume Instructions

1. Read `PLAN.md` and this file.
2. Inspect the current project tree and any uncommitted/user changes before editing.
3. Run the narrow validation for the current in-progress slice.
4. Update this file with exact files, commands, results, blockers, and the next action at each phase boundary and before stopping.
5. Never infer success for work or tests not explicitly recorded here.