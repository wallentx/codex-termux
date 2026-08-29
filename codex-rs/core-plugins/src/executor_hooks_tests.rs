use std::collections::HashMap;
use std::sync::Arc;

use codex_config::HookEventsToml;
use codex_config::MatcherGroup;
use codex_exec_server::CapabilityRootDiscovery;
use codex_exec_server::CapabilityTextFile;
use codex_exec_server::DiscoveredPluginFiles;
use codex_exec_server::ExecutorCapabilityDiscoverySnapshot;
use codex_plugin::ExecutorPluginHookSource;
use codex_protocol::capabilities::CapabilityRootLocation;
use codex_protocol::capabilities::SelectedCapabilityRoot;
use codex_utils_path_uri::PathUri;
use pretty_assertions::assert_eq;
use serde_json::json;

use super::*;

fn snapshot_for_manifest(
    plugin_id: &str,
    environment_id: &str,
    manifest_path: &str,
    manifest: serde_json::Value,
) -> ExecutorCapabilityDiscoverySnapshot {
    let plugin_root = PathUri::parse("file:///plugins/computer-use").expect("plugin root");
    let manifest_path = PathUri::parse(manifest_path).expect("manifest path");
    let selected_root = SelectedCapabilityRoot {
        id: plugin_id.to_string(),
        location: CapabilityRootLocation::Environment {
            environment_id: environment_id.to_string(),
            path: plugin_root.clone(),
        },
    };
    let discovery = CapabilityRootDiscovery {
        id: plugin_id.to_string(),
        path: plugin_root,
        plugin: Some(DiscoveredPluginFiles {
            manifest: CapabilityTextFile {
                path: manifest_path,
                contents: manifest.to_string(),
            },
            mcp_config: None,
            apps_config: None,
        }),
        skills: Vec::new(),
        namespace_manifests: Vec::new(),
        warnings: Vec::new(),
        error: None,
    };
    ExecutorCapabilityDiscoverySnapshot::new(
        &[selected_root],
        vec![Ok(Arc::new(discovery))],
        HashMap::new(),
    )
}

fn cleanup_hook_manifest() -> serde_json::Value {
    json!({
        "name": "computer-use",
        "hooks": {
            "hooks": {
                "Stop": [{
                    "hooks": [{
                        "type": "mcp_tool",
                        "server": "node_repl",
                        "tool": "turn_ended",
                        "input": {
                            "hook_event_name": "${hook_event_name}",
                            "session_id": "${session_id}",
                            "turn_id": "${turn_id}"
                        }
                    }]
                }]
            }
        }
    })
}

fn expected_source(index: usize) -> ExecutorPluginHookSource {
    ExecutorPluginHookSource {
        plugin_id: PluginId::parse("computer-use@openai-bundled").expect("plugin id"),
        environment_id: "executor-a".to_string(),
        plugin_root: PathUri::parse("file:///plugins/computer-use").expect("plugin root"),
        manifest_path: PathUri::parse("file:///plugins/computer-use/.codex-plugin/plugin.json")
            .expect("manifest path"),
        source_relative_path: format!(".codex-plugin/plugin.json#hooks[{index}]"),
        hooks: HookEventsToml {
            stop: vec![MatcherGroup {
                matcher: None,
                hooks: vec![HookHandlerConfig::McpTool {
                    server: "node_repl".to_string(),
                    tool: "turn_ended".to_string(),
                    input: serde_json::from_value(json!({
                        "hook_event_name": "${hook_event_name}",
                        "session_id": "${session_id}",
                        "turn_id": "${turn_id}",
                    }))
                    .expect("executor hook input"),
                    timeout_sec: None,
                    status_message: None,
                }],
            }],
            ..Default::default()
        },
    }
}

#[test]
fn discovers_allowlisted_executor_plugin_hook_sources() {
    let cleanup_hooks = cleanup_hook_manifest()["hooks"]["hooks"]["Stop"].clone();
    let manifest = json!({
        "name": "computer-use",
        "hooks": [
            { "hooks": { "Stop": cleanup_hooks } },
            { "hooks": { "Interrupt": cleanup_hooks } },
            { "hooks": {
                "Stop": cleanup_hooks,
                "Interrupt": cleanup_hooks,
                "UserPromptSubmit": cleanup_hooks
            } },
            { "hooks": { "UserPromptSubmit": cleanup_hooks } }
        ]
    });
    let snapshot = snapshot_for_manifest(
        "computer-use@openai-bundled",
        "executor-a",
        "file:///plugins/computer-use/.codex-plugin/plugin.json",
        manifest,
    );

    let sources = executor_plugin_hook_sources(&snapshot);
    let mut interrupt_source = expected_source(/*index*/ 1);
    interrupt_source.hooks.interrupt = std::mem::take(&mut interrupt_source.hooks.stop);
    let mut stop_and_interrupt_source = expected_source(/*index*/ 2);
    stop_and_interrupt_source.hooks.interrupt = stop_and_interrupt_source.hooks.stop.clone();

    assert_eq!(
        sources,
        vec![
            expected_source(/*index*/ 0),
            interrupt_source,
            stop_and_interrupt_source,
        ]
    );
}

#[test]
fn filters_mixed_handlers_without_rewriting_allowed_groups() {
    let mut expected = expected_source(/*index*/ 0);
    let mut second_handler = expected.hooks.stop[0].hooks[0].clone();
    let HookHandlerConfig::McpTool { input, .. } = &mut second_handler else {
        panic!("expected an MCP tool hook");
    };
    input.insert("order".to_string(), json!(2));
    expected.hooks.stop[0].hooks.push(second_handler.clone());
    expected.hooks.stop.push(MatcherGroup {
        matcher: None,
        hooks: vec![second_handler],
    });

    let mut manifest = cleanup_hook_manifest();
    manifest["hooks"]["hooks"]["Stop"] =
        serde_json::to_value(&expected.hooks.stop).expect("serialize stop hooks");
    manifest["hooks"]["hooks"]["Stop"][0]["hooks"]
        .as_array_mut()
        .expect("stop handlers")
        .insert(
            /*index*/ 0,
            json!({ "type": "mcp_tool", "server": "node_repl", "tool": "other" }),
        );
    let snapshot = snapshot_for_manifest(
        "computer-use@openai-bundled",
        "executor-a",
        "file:///plugins/computer-use/.codex-plugin/plugin.json",
        manifest,
    );

    assert_eq!(executor_plugin_hook_sources(&snapshot), vec![expected]);
}

#[test]
fn preserves_allowlisted_executor_plugin_hook_options() {
    let mut manifest = cleanup_hook_manifest();
    let handler = &mut manifest["hooks"]["hooks"]["Stop"][0]["hooks"][0];
    handler["input"] = json!({ "untrusted": "manifest-provided input" });
    handler["timeout"] = json!(30);
    handler["statusMessage"] = json!("Cleaning up Computer Use");
    manifest["hooks"]["hooks"]["Interrupt"] = manifest["hooks"]["hooks"]["Stop"].clone();
    let snapshot = snapshot_for_manifest(
        "computer-use@openai-bundled",
        "executor-a",
        "file:///plugins/computer-use/.codex-plugin/plugin.json",
        manifest,
    );

    let mut expected = expected_source(/*index*/ 0);
    expected.hooks.stop[0].hooks[0] = HookHandlerConfig::McpTool {
        server: "node_repl".to_string(),
        tool: "turn_ended".to_string(),
        input: serde_json::from_value(json!({ "untrusted": "manifest-provided input" }))
            .expect("manifest-provided executor hook input"),
        timeout_sec: Some(30),
        status_message: Some("Cleaning up Computer Use".to_string()),
    };
    expected.hooks.interrupt = expected.hooks.stop.clone();

    assert_eq!(executor_plugin_hook_sources(&snapshot), vec![expected]);
}

#[test]
fn ignores_unallowlisted_executor_plugin_hooks() {
    let mut wrong_event = cleanup_hook_manifest();
    let hook_events = wrong_event["hooks"]["hooks"]
        .as_object_mut()
        .expect("hook events");
    let stop_groups = hook_events.remove("Stop").expect("stop groups");
    hook_events.insert("SessionStart".to_string(), stop_groups);
    let mut wrong_handler = cleanup_hook_manifest();
    wrong_handler["hooks"]["hooks"]["Stop"][0]["hooks"][0] = json!({
        "type": "command",
        "command": "echo cleanup"
    });
    let mut wrong_server = cleanup_hook_manifest();
    wrong_server["hooks"]["hooks"]["Stop"][0]["hooks"][0]["server"] = json!("other");
    let mut wrong_tool = cleanup_hook_manifest();
    wrong_tool["hooks"]["hooks"]["Stop"][0]["hooks"][0]["tool"] = json!("other");
    let mut wrong_matcher = cleanup_hook_manifest();
    wrong_matcher["hooks"]["hooks"]["Stop"][0]["matcher"] = json!("unexpected");
    for (name, plugin_id, manifest) in [
        (
            "unallowlisted bundled plugin",
            "another-plugin@openai-bundled",
            cleanup_hook_manifest(),
        ),
        (
            "wrong marketplace",
            "computer-use@other",
            cleanup_hook_manifest(),
        ),
        (
            "bundled alpha marketplace",
            "computer-use@openai-bundled-alpha",
            cleanup_hook_manifest(),
        ),
        (
            "bundled marketplace suffix",
            "computer-use@fake-openai-bundled",
            cleanup_hook_manifest(),
        ),
        ("wrong event", "computer-use@openai-bundled", wrong_event),
        (
            "wrong handler",
            "computer-use@openai-bundled",
            wrong_handler,
        ),
        ("wrong server", "computer-use@openai-bundled", wrong_server),
        ("wrong tool", "computer-use@openai-bundled", wrong_tool),
        (
            "wrong matcher",
            "computer-use@openai-bundled",
            wrong_matcher,
        ),
    ] {
        let snapshot = snapshot_for_manifest(
            plugin_id,
            "executor-a",
            "file:///plugins/computer-use/.codex-plugin/plugin.json",
            manifest,
        );

        assert_eq!(
            executor_plugin_hook_sources(&snapshot),
            Vec::<ExecutorPluginHookSource>::new(),
            "{name}"
        );
    }
}

#[test]
fn ignores_file_backed_executor_plugin_hooks() {
    let file_backed = snapshot_for_manifest(
        "computer-use@openai-bundled",
        "executor-a",
        "file:///plugins/computer-use/.codex-plugin/plugin.json",
        json!({
            "name": "computer-use",
            "hooks": "./hooks/hooks.json"
        }),
    );

    assert_eq!(
        executor_plugin_hook_sources(&file_backed),
        Vec::<ExecutorPluginHookSource>::new()
    );
}
