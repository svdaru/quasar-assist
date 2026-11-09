"""Константы интеграции Quasar Assist."""

DOMAIN = "quasar_assist"

CONF_PIPELINE_ID = "pipeline_id"
CONF_CONVERSATION_TTL = "conversation_ttl"
DEFAULT_CONVERSATION_TTL = 90

RESPONSES_URL = "/api/quasar_assist/v1/responses"
MODELS_URL = "/api/quasar_assist/v1/models"
# Короткий запрос моста, история, tools и системный текст остаются ниже предела.
MAX_REQUEST_BODY_BYTES = 1024 * 1024
RESPONSES_VIEW_NAME = "api:quasar_assist:responses"
MODELS_VIEW_NAME = "api:quasar_assist:models"

PHRASE_NOT_FOUND = "Запись не найдена"
PHRASE_EMPTY = "Пустая фраза"
PHRASE_FAILED = "Не удалось выполнить"

ERROR_PIPELINE_NOT_FOUND = "pipeline_not_found"
ERROR_INVALID_TTL = "invalid_ttl"
ABORT_NO_PIPELINES = "no_pipelines"

ISSUE_PIPELINE_UNAVAILABLE = "pipeline_unavailable"
# Идентификатор repair версии 0.1.0. Новый код его только снимает.
LEGACY_ISSUE_ASSISTANT_UNAVAILABLE = "assistant_unavailable"
