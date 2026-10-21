# Configuration

For basic configuration instructions, see [this documentation](https://developers.openai.com/codex/config-basic).

For advanced configuration instructions, see [this documentation](https://developers.openai.com/codex/config-advanced).

For a full configuration reference, see [this documentation](https://developers.openai.com/codex/config-reference).

## Lifecycle hooks

Admins can set top-level `allow_managed_hooks_only = true` in
`requirements.toml` to ignore user, project, and session hook configs while
still allowing managed hooks from requirements and managed config layers. This
setting is only supported in `requirements.toml`; putting it in `config.toml`
does not enable managed-hooks-only mode.

## Android local MCP processes

On Android, local stdio MCP servers inherit `LD_PRELOAD`,
`TERMUX_EXEC__SYSTEM_LINKER_EXEC__MODE`, and
`TERMUX_EXEC__EXECVE_CALL__INTERCEPT` when those variables are set in Codex's
parent environment. This preserves the existing Termux runtime and execution
policy without requiring Aether, Shizuku, or a particular library path. Unset
variables remain unset. Explicit MCP `env` values override inherited values,
including an empty `LD_PRELOAD`.

`TERMUX_EXEC__PROC_SELF_EXE` is not inherited by default because it identifies
the parent process. Remote stdio MCP servers retain their executor-side runtime
environment; these Android defaults apply only to local launches.
