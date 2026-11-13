# Trnje Termux Dropbox inbox

The Dropbox input is an optional transport into the existing GitHub Issue #3 mailbox. It does not execute Dropbox content directly.

## Enablement

Set \`TRNJE_DROPBOX_ENABLED=1\` in the environment loaded by the existing Trnje worker service. The default is disabled, so an update does not change current polling behavior.

The worker reads the same Dropbox app credentials used by \`trnje-publish\` from \`~/.config/emberlane/dropbox.env\` (or \`TRNJE_DROPBOX_CONFIG\` / \`EMBERLANE_DROPBOX_CONFIG\`). Required keys are \`DROPBOX_APP_KEY\`, \`DROPBOX_APP_SECRET\`, and \`DROPBOX_REFRESH_TOKEN\`. Keep that file mode \`0600\`. The inbox path defaults to \`/trnje-inbox\`, relative to the Dropbox app folder; in the Dropbox UI this normally appears under \`/Apps/Emberlane Worker/trnje-inbox\`. Override with \`TRNJE_DROPBOX_INBOX\` only when necessary.

The worker does not create the folder. Create the inbox folder before enabling the feature. Keep it private and do not share write access: anyone able to add a command file can request an allowlisted operation.

## Command file

Upload a UTF-8 file whose name ends in \`.json\` and whose complete contents use the existing mailbox format:

\`\`\`text
TRNJE_TASK_JSON
{"kind":"diagnostics","payload":{}}
\`\`\`

The existing parser and allowlist remain authoritative. Supported kinds and payloads are unchanged. Arbitrary shell commands, unknown fields, malformed JSON, oversized files and non-UTF-8 files are rejected. Files larger than 16 KiB are rejected.

The worker forwards a validated file as a comment on the existing GitHub Issue #3. The actual GitHub comment author is the worker's authenticated GitHub account, which is still checked against \`ACTORS\` by the existing process path. A hidden source marker derived from Dropbox file ID + revision prevents duplicate forwarding after retries or restarts. Dropbox cursor state is stored alongside the existing worker state; the GitHub task-state schema is not reset.

Processed files are left in the inbox. Files are not deleted or moved automatically. Non-\`.json\` files are ignored. A malformed or disallowed \`.json\` blocks the Dropbox intake cursor so it cannot silently skip a command; GitHub Issue #3 polling continues, and the offending file must be corrected or removed.

## Failure isolation

Dropbox authentication, folder, network or malformed-file errors are recorded as \`last_dropbox_poll_status=BLOCKED\` and do not stop GitHub Issue #3 polling. When \`TRNJE_DROPBOX_ENABLED\` is unset or not \`1\`, the Dropbox input path is not contacted.
