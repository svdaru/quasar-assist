"""Константы интеграции Quasar Assist."""

DOMAIN = "quasar_assist"

CONF_AGENT_ID = "agent_id"
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

ERROR_ASSISTANT_NOT_FOUND = "assistant_not_found"
ERROR_INVALID_TTL = "invalid_ttl"
ABORT_NO_ASSISTANTS = "no_assistants"

ISSUE_ASSISTANT_UNAVAILABLE = "assistant_unavailable"
