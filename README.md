# Quasar Assist

Quasar Assist is a Home Assistant integration that accepts an OpenAI Responses request from Quasar Bridge 0.7.5 and passes the last user phrase to the built-in Assist dialogue. Assist performs the command. The bridge speaks the reply. This integration does not call an external language model, does not forward the original request, and does not execute bridge tools.

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

Quasar Bridge 0.7.5 has one model address for the `llm` route, the `home` scheduler, and the classifier. This integration sends every last user phrase to Assist, so point only the phrases that should become Assist commands at this address.

- Base URL: `http://<home-assistant>:8123/api/quasar_assist/v1`
- API type: `responses` (`llm_api` stays `responses`)
- `llm_base_url` points at the base URL above.
- `llm_model` is the `entry_id` of the Quasar Assist entry you want. The installer copies it from the `id` field of `GET /api/quasar_assist/v1/models`. Each speaker uses its own `llm_model` when the entries differ.
- `QUASAR_LLM_API_KEY` on the speaker is the Home Assistant long-lived access token.

Routing example for the wake name used in this setup:

- A rule `starts_with_any` of `львовна `, `львовна,`, `львовна.` sends the phrase to the `llm` route. The rule sees the phrase before the bridge strips a prefix.
- Add `львовна` to `llm_strip_prefixes` together with the prefixes you already keep. A custom list replaces the standard list entirely and starts working after the bridge restarts. The bridge then sends only the remainder, for example `включи свет`.
- While the bridge has a single model address, do not send the `home` scheduler, the classifier, or "ask the neural net" to this URL. Those need the previous model address.

## Removal

In Home Assistant, open Settings, then Devices & services, then Quasar Assist, and delete the integration entry. You can also remove the repository from the Marketplace. Deleting an entry removes its dialogue state and its repair, if one was open. Nothing in this integration is stored outside the Home Assistant config entry.

## Parameters

### Installation parameters

Created when you add the integration:

| Parameter | Required | Default | Meaning |
| --- | --- | --- | --- |
| Voice assistant | yes | none | A voice assistant that is already set up in Home Assistant. The list is not filtered. There is no free-text `agent_id` and no default, so an entry cannot be created without an assistant. The same assistant may be selected again; `entry_id` keeps the entries distinct. |
| Dialogue lifetime | no | 90 seconds | How long the Assist `conversation_id` of this entry stays open after a successful reply. |

No separate identifier is requested. Home Assistant already assigns `entry_id`.

### Configuration parameters

The same two fields can be changed later without deleting the entry. The next phrase uses the new assistant. The new lifetime is stored on the entry and limits its timer. `conversation_id` and the time of the last successful reply live in `runtime_data`, not in the stored settings.

## Examples

`POST /api/quasar_assist/v1/responses` with the entry id in `model`. Only the last `user` string is sent to Assist. System text, older turns, `tools`, and `current_time` stay in the request and are not copied into Assist.

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

A successful reply is HTTP 200. The speech is the plain text Assist returned. The integration does not add a `function_call` item.

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

`GET /api/quasar_assist/v1/models` returns the current Quasar Assist entries. `id` is `entry_id`. `name` is the entry title, or the same `id` when the title is empty. The list is ordered by `id`.

```json
{
  "data": [
    {"id": "01JEXAMPLEENTRYIDBEDROOM", "name": "Спальня"}
  ]
}
```

## Supported functions

- Accept `POST /api/quasar_assist/v1/responses` from Quasar Bridge 0.7.5.
- Pass the last `user` phrase to `conversation.async_converse` unchanged, with the entry's assistant and the dialogue id chosen by that entry's timer.
- Return Assist plain speech as a completed assistant message, including when the Assist response type is an error but the speech is not empty.
- List config entries at `GET /api/quasar_assist/v1/models` without calling Assist and without changing dialogue timers.
- Keep a separate dialogue timer for each entry. The default lifetime is 90 seconds from the last successful reply, and each success extends it.
- Reconfigure the assistant and the lifetime without deleting the entry.
- Raise a repair when the selected assistant disappears, and log the loss once and the recovery once.

## Use case

A person says "Алиса Львовна, включи свет". The speaker wake word removes "Алиса". Quasar Bridge 0.7.5 sees "Львовна, включи свет", strips "Львовна" because it is in `llm_strip_prefixes`, and posts "включи свет" to this integration. Assist runs the command and returns speech. The bridge speaks that speech. A later phrase for the same entry, such as "А в спальне" or "включи телевизор", continues the same Assist dialogue until the entry timer expires.

## Known limitations

- Text only. The integration does not accept audio, does not replace the "Алиса" wake word, and does not synthesize speech. The speaker speaks the text through the bridge.
- The supported client is Quasar Bridge 0.7.5. The integration does not change the bridge, its routing rules, or speaker firmware.
- There is no outgoing language-model call, no MCP call, and no execution of bridge `tools`.
- Dialogue continuation is only the per-entry timer. Words such as "да", "нет", and a phrase that starts with "а " do not start or stop a dialogue by themselves.
- The integration does not trim the last user phrase. If the bridge did not strip a prefix, the prefix is sent to Assist as received.
- An empty `data` array is returned only when there are no entries. Quasar Bridge treats an empty model catalog as an error and will not let you pick a model.
- The bridge has one model address. Do not point the `home` scheduler, the classifier, or "ask the neural net" at this integration.
- Language is not sent to Assist. Assist uses the Home Assistant server language.
- There is no `quality_scale` key in the manifest. Marketplace integrations are shown as `custom`.

## Troubleshooting

- **HTTP 401.** The long-lived access token is missing, expired, or not accepted by Home Assistant. Create a new token and update `QUASAR_LLM_API_KEY`. This integration does not log the token or the `Authorization` header.
- **The bridge says the entry was not found.** `model` is empty, unknown, or belongs to another integration. Open `GET /api/quasar_assist/v1/models` and copy `id` into `llm_model`.
- **A short reply says the phrase is empty.** The request had no `user` message, or the last `user` text was empty. Assist is not called, and an open dialogue is left as it is.
- **A short Russian reply says the request failed.** The selected assistant is gone, Assist returned empty speech, or the Assist call raised. The dialogue for that entry is closed. If the assistant was removed, Home Assistant shows a repair on the entry; reconfigure the entry and choose an assistant. The loss is logged once, and the recovery is logged once.
- **The body is rejected with HTTP 400.** `input` is missing or is not a list, or the last `user` content is not a string. Assist is not called.
- **The speaker stays silent.** The bridge rejects a reply that contains tool-call markup. This integration returns only plain `output_text` and does not add a `function_call`.
