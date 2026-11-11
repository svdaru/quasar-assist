# Quasar Assist

Quasar Assist is a Home Assistant integration that accepts an OpenAI Responses request from Quasar Bridge 0.7.5 and passes the last user phrase to a selected Assist pipeline. The pipeline performs the command. The bridge speaks the reply. This integration does not call an external language model, does not forward the original request, and does not execute bridge tools.

Russian documentation: [README.ru.md](README.ru.md).

## Installation

Install Quasar Assist from the Home Assistant Marketplace.

1. Open the Marketplace and add the custom repository `https://github.com/svdaru/quasar-assist`.
2. Install **Quasar Assist**.
3. Home Assistant loads a config-flow integration on first install without a restart. An update of an already loaded integration needs the restart that the Marketplace offers.

Home Assistant 2026.3 or newer is required. From that version Home Assistant serves the local icon in `custom_components/quasar_assist/brand/`.

## Long-lived access token

The bridge authenticates with a Home Assistant long-lived access token. Create one in your user profile and put it in `QUASAR_LLM_API_KEY` on the speaker. Both integration URLs use Home Assistant's normal check of that token (`requires_auth`). A missing or unknown token is rejected with HTTP 401 before this integration runs.

Quasar Assist does not create a second secret, does not store the token in the config entry, and does not read the `Authorization` header itself.

## Bridge setup

The bridge setup below is not performed by this integration. Without it, the speaker will not send phrases.

Quasar Bridge 0.7.5 has one model address for the `llm` route, the `home` scheduler, and the classifier. This integration sends every last user phrase to the selected Assist pipeline, so point only the phrases that should become Assist commands at this address. A phrase whose first system message is exactly `classify` does not go to the pipeline.

- Base URL: `http://<home-assistant>:8123/api/quasar_assist/v1`
- API type: `responses` (`llm_api` stays `responses`)
- `llm_base_url` points at the base URL above.
- `llm_model` is the `entry_id` of the Quasar Assist entry you want. The installer copies it from the `id` field of `GET /api/quasar_assist/v1/models`. Each speaker uses its own `llm_model` when the entries differ.
- `QUASAR_LLM_API_KEY` on the speaker is the Home Assistant long-lived access token.

Routing example for the wake name used in this setup:

- A rule `starts_with_any` of `львовна `, `львовна,`, `львовна.` sends the phrase to the `llm` route. The rule sees the phrase before the bridge strips a prefix.
- Add `львовна` to `llm_strip_prefixes` together with the prefixes you already keep. A custom list replaces the standard list entirely and starts working after the bridge restarts. The bridge then sends only the remainder, for example `включи свет`.
- A request whose first system message is exactly `classify` is answered here with the route `llm` or `yandex` and does not run the pipeline. This integration does not return the route `home`. While the bridge has a single model address, the home scheduler still needs the previous model address.

## Removal

In Home Assistant, open Settings, then Devices & services, then Quasar Assist, and delete the integration entry. You can also remove the repository from the Marketplace. Deleting an entry removes its dialogue state and its repair, if one was open. Nothing in this integration is stored outside the Home Assistant config entry.

## Parameters

### Installation parameters

Created when you add the integration:

| Parameter | Required | Default | Meaning |
| --- | --- | --- | --- |
| Assist pipeline | yes | none | An Assist pipeline that is already set up in Home Assistant. The list is not filtered. There is no free-text pipeline id and no default, so an entry cannot be created without a pipeline. The same pipeline may be selected again; `entry_id` keeps the entries distinct. |
| Dialogue lifetime | no | 90 seconds | How long the `conversation_id` of this entry stays open after a successful reply. |

No separate identifier is requested. Home Assistant already assigns `entry_id`.

### Configuration parameters

The same two fields can be changed later without deleting the entry. The next phrase uses the new pipeline and starts a new dialogue. The new lifetime is stored on the entry and limits its timer. `conversation_id` and the time of the last successful reply live in `runtime_data`, not in the stored settings.

## Examples

`POST /api/quasar_assist/v1/responses` with the entry id in `model`. Only the last `user` string is sent to the pipeline as `intent_input`. The run starts and ends at the intent stage, so speech recognition and speech synthesis of that pipeline are not started. System text, older turns, `tools`, `current_time`, and `text.format` stay in the request and are not copied into the run. Language is not sent separately; the pipeline uses its own language.

```json
{
  "model": "01JEXAMPLEENTRYIDBEDROOM",
  "input": [
    {"role": "system", "content": "Bridge tool instructions"},
    {"role": "user", "content": "включи свет"},
    {"role": "assistant", "content": "В какой комнате?"},
    {"role": "user", "content": "А в спальне"}
  ]
}
```

A successful reply is HTTP 200. The speech is the plain text from the pipeline `intent-end` event. Non-empty speech is success even when the response type is an error. The integration does not add a `function_call` item.

```json
{
  "status": "completed",
  "output": [
    {
      "type": "message",
      "role": "assistant",
      "status": "completed",
      "content": [
        {"type": "output_text", "text": "Включила свет"}
      ]
    }
  ]
}
```

`GET /api/quasar_assist/v1/models` returns the current Quasar Assist entries. `id` is `entry_id`. `name` is the entry title, or the same `id` when the title is empty. The list is ordered by `id`. The pipeline is not started and dialogue timers do not change.

```json
{
  "data": [
    {"id": "01JEXAMPLEENTRYIDBEDROOM", "name": "Спальня"}
  ]
}
```

## Supported functions

- Accept `POST /api/quasar_assist/v1/responses` from Quasar Bridge 0.7.5.
- Pass the last `user` phrase unchanged to the entry's Assist pipeline through `PipelineInput`, with `start_stage` and `end_stage` at intent. The dialogue id is chosen by that entry's timer.
- Return the `intent-end` speech as a completed assistant message, including when the response type is an error but the speech is not empty.
- List config entries at `GET /api/quasar_assist/v1/models` without starting a pipeline and without changing dialogue timers.
- Keep a separate dialogue timer for each entry. The default lifetime is 90 seconds from the last successful reply, and each success extends it.
- Reconfigure the pipeline and the lifetime without deleting the entry. The next call uses the new pipeline and starts a new dialogue.
- Raise a repair when the selected pipeline disappears, and log the loss once and the recovery once. An older entry that has no pipeline id does not start until a pipeline is chosen again.
- Answer a first system message of exactly `classify` with the route `llm` or `yandex` and do not run the pipeline. The route `home` is not returned.

## Use case

A person says "Алиса Львовна, включи свет". The speaker wake word removes "Алиса". Quasar Bridge 0.7.5 sees "Львовна, включи свет", strips "Львовна" because it is in `llm_strip_prefixes`, and posts "включи свет" to this integration. The selected Assist pipeline runs the command and returns speech. The bridge speaks that speech. A later phrase for the same entry, such as "А в спальне" or "включи телевизор", continues the same dialogue until the entry timer expires.

## Known limitations

- Text only. The integration does not accept audio, does not replace the "Алиса" wake word, and does not synthesize speech. The speaker speaks the text through the bridge. Speech recognition and synthesis configured on the pipeline are not started.
- The supported client is Quasar Bridge 0.7.5. The integration does not change the bridge, its routing rules, or speaker firmware.
- There is no outgoing language-model call, no MCP call, and no execution of bridge `tools`.
- Dialogue continuation is only the per-entry timer. Words such as "да", "нет", and a phrase that starts with "а " do not start or stop a dialogue by themselves.
- The integration does not trim the last user phrase. If the bridge did not strip a prefix, the prefix is sent to the pipeline as received.
- An empty `data` array is returned only when there are no entries. Quasar Bridge treats an empty model catalog as an error and will not let you pick a model.
- The bridge has one model address. A system message `classify` is answered with `llm` or `yandex` without the pipeline. This integration does not return the route `home`.
- Language is not sent separately. The pipeline uses its own language.
- The integration does not bind the speaker to a Home Assistant device or satellite.
- There is no `quality_scale` key in the manifest. Marketplace integrations are shown as `custom`.

## Classify route

If the first `system` message is the string `classify` with no spaces around it, Quasar Assist does not run the Assist pipeline. The reply is still HTTP 200 and the same completed message. `output_text` is a JSON object whose only field is `route`. The built-in agent checks registered `conversation` triggers first, including triggers of scripts, and then a strict intent. A match gives `llm`. Otherwise the value is `yandex`. This integration does not return the route `home`. A match does not execute the automation, the script, or the intent, and it does not change the dialogue timer. The route does not depend on whether the selected pipeline is available. The pipeline's own agent is not called.

An unknown entry answers before that logic: HTTP 200 and a short phrase, including when the phrase is empty and when the system message is `classify`. The agent methods are not started.

When the entry is found, an empty phrase on this route answers HTTP 500 with an empty body. The same empty HTTP 500 is returned when the built-in agent is missing or when either of the two recognition methods raises any other exception. `asyncio.CancelledError`, `KeyboardInterrupt`, `SystemExit`, and `GeneratorExit` are not this failure. The body is empty: not JSON and not a short phrase. The pipeline is not started, and the dialogue is left as it was.

The last user phrase and the pipeline reply speech are written only in one debug line for each `POST /api/quasar_assist/v1/responses`. Information, warning, error, and the downloadable diagnostics do not contain them. In Home Assistant open Settings, then System, then Logs, and search for `quasar_assist`.

## Troubleshooting

- **HTTP 401.** The long-lived access token is missing, expired, or not accepted by Home Assistant. Create a new token and update `QUASAR_LLM_API_KEY`. This integration does not log the token or the `Authorization` header.
- **The bridge says the entry was not found.** `model` is empty, unknown, or belongs to another integration. Open `GET /api/quasar_assist/v1/models` and copy `id` into `llm_model`.
- **A short reply says the phrase is empty.** Without `classify`, the request had no `user` message, or the last `user` text was empty. The pipeline is not started, and an open dialogue is left as it is. When the entry is found and the first system message is exactly `classify`, the same empty phrase is HTTP 500 with an empty body.
- **A short Russian reply says the request failed.** The selected pipeline is gone, the pipeline returned empty speech, or the run failed. The dialogue for that entry is closed. There is no second attempt. If the pipeline was removed, or the entry was created before a pipeline was stored, Home Assistant shows a repair; reconfigure the entry and choose a pipeline. The loss is logged once, and the recovery is logged once.
- **The body is rejected with HTTP 400.** `input` is missing or is not a list, or the last `user` content is not a string. The pipeline is not started. An empty `user` string is not this error.
- **The speaker stays silent.** The bridge rejects a reply that contains tool-call markup. This integration returns only plain `output_text` and does not add a `function_call`.

## License

Apache License 2.0. The terms are in [LICENSE](LICENSE).
