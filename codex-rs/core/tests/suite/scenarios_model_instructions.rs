//! Instruction refresh and compaction with an unchanged model and tool catalog.

use super::*;
use codex_models_manager::manager::RefreshStrategy;
use codex_models_manager::model_info::model_info_from_slug;
use codex_protocol::openai_models::ModelVisibility;
use codex_protocol::openai_models::ModelsResponse;
use core_test_support::responses;

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn catalog_refresh_replaces_instructions_and_compaction_installs_latest_base() -> Result<()> {
    skip_if_no_network!(Ok(()));

    let server = wiremock::MockServer::start().await;
    let mut model = model_info_from_slug("gpt-5.4");
    model.use_responses_lite = true;
    model.visibility = ModelVisibility::List;
    model
        .model_messages
        .get_or_insert_default()
        .instructions_template = Some("Help the user complete their task.".to_string());
    responses::mount_models_once_with_etag(
        &server,
        ModelsResponse {
            models: vec![model.clone()],
        },
        "\"instructions-v1\"",
    )
    .await;
    let mock = mount_sse_sequence(
        &server,
        (1..=5)
            .map(|index| {
                let mut events = Vec::new();
                if index == 4 {
                    events.push(json!({
                        "type": "response.output_item.done",
                        "item": {
                            "type": "compaction",
                            "encrypted_content": "MODEL_INSTRUCTIONS_CHECKPOINT",
                        },
                    }));
                }
                events.push(ev_completed(&format!("resp-{index}")));
                sse(events)
            })
            .collect(),
    )
    .await;
    let test = test_codex()
        .with_model(&model.slug)
        .with_auth(CodexAuth::create_dummy_chatgpt_auth_for_testing())
        .with_config(|config| {
            config.workspace_roots = vec![config.cwd.clone()];
            config
                .features
                .enable(Feature::IncrementalTools)
                .expect("enable incremental tools");
        })
        .build_with_auto_env(&server)
        .await?;
    test.submit_turn("Begin work.").await?;

    model
        .model_messages
        .get_or_insert_default()
        .instructions_template = Some("Check your work before responding.".to_string());
    responses::mount_models_once_with_etag(
        &server,
        ModelsResponse {
            models: vec![model],
        },
        "\"instructions-v2\"",
    )
    .await;
    test.thread_manager
        .get_models_manager()
        .list_models(
            RefreshStrategy::Online,
            codex_core::test_support::default_http_client_factory(),
        )
        .await;
    test.submit_turn("Continue with refreshed instructions.")
        .await?;
    test.submit_turn("Continue without further changes.")
        .await?;
    test.codex.submit(Op::Compact).await?;
    wait_for_event(&test.codex, |event| {
        assert!(!matches!(event, EventMsg::Error(_)), "{event:?}");
        matches!(event, EventMsg::TurnComplete(_))
    })
    .await;
    test.submit_turn("Continue after compaction.").await?;

    insta::assert_snapshot!(
        "incremental_model_instructions",
        context_snapshot::format_request_history_snapshot(
            "A same-model catalog refresh appends replacement instructions once without changing tools or the prefix. Compaction starts a new window with the latest base.",
            &mock.requests(),
            &ContextSnapshotOptions::default().include_request_settings(),
        )
    );
    Ok(())
}
