# Shared body for the xuangu auto-answer flow.
# Wrappers source this after setting the account env, XIU_FLOW_SLUG, and
# TG_TRIGGER_TARGET_USERNAME.
export TG_FLOW_SLUG="${XIU_FLOW_SLUG}"
# Do NOT reuse the latest successful letter blindly: Xuangu shuffles options.
# Reply only when the live question/options contain a known correct answer text.
export TG_AUTO_REPLY_MODE="${TG_AUTO_REPLY_MODE:-answer_bank_option_text}"
export TG_AUTO_REPLY_MESSAGE="${TG_AUTO_REPLY_MESSAGE:-.作答 A}"
export TG_DYNAMIC_REPLY_TEMPLATE="${TG_DYNAMIC_REPLY_TEMPLATE:-.作答 {option}}"
export TG_DYNAMIC_OPTION_PATTERN="${TG_DYNAMIC_OPTION_PATTERN:-^\\s*([A-D])\\s*[\\.．、:：]\\s*(.+)$}"
# Known answer aliases are usable only for their matching question stem. New
# official answers are learned per question, never as a global answer word.
export TG_ANSWER_BANK_TEXT="${TG_ANSWER_BANK_TEXT:-虚天鼎|虛天鼎|鼎外乾蓝冰焰|鼎外乾藍冰焰|乾蓝冰焰|乾藍冰焰|辟邪神雷|血玉蜘蛛|元婴后|元嬰後|修罗圣火|先得虚天鼎，后把乾蓝自鼎上抽离|冰凤寒性与偏火残焰同炉|独立法宝|紫罗极火|鼎焰反噬}"
export TG_ANSWER_BANK_PATH="${TG_ANSWER_BANK_PATH:-state/xuangu_answer_bank.json}"
export TG_AUTO_UPDATE_ANSWER_BANK="${TG_AUTO_UPDATE_ANSWER_BANK:-1}"
export TG_LEARNED_ANSWER_RESULT_MARKER_TEXT="${TG_LEARNED_ANSWER_RESULT_MARKER_TEXT:-玄骨|答案|完全正}"
export TG_LEARNED_ANSWER_RESULT_PATTERN="${TG_LEARNED_ANSWER_RESULT_PATTERN:-答案\\s*([A-D])\\s*完全正[确確]}"
export TG_LEARNED_ANSWER_MAX_AGE_SECONDS="${TG_LEARNED_ANSWER_MAX_AGE_SECONDS:-86400}"
# Match both the normal and rare richer Xuangu events. Route by explicit @username.
export TG_TRIGGER_MARKER_TEXT="${TG_TRIGGER_MARKER_TEXT:-神念直入脑海|玄骨||天机异象|玄骨||天機異象|玄骨||乾蓝冰焰是何时|300秒||乾藍冰焰是何時|300秒}"
export TG_TRIGGER_TRUSTED_SENDER_IDS="${TG_TRIGGER_TRUSTED_SENDER_IDS:-7228205882,7965897083,8735907987,8816935632,8918867302,8944702077,8964348409,8980154525,8788895566,8917921351,8388633812,8861328042}"
# Choices must reply to the original question in the canonical topic. The
# secondary group remains a read-only source for attributable result receipts.
export TG_TOPIC_MATCH_MODE='top'
export TG_SCAN_LIMIT="${TG_SCAN_LIMIT:-360}"
# Xuangu question expires quickly; avoid late answers after 题面失效/超时.
export TG_TRIGGER_MAX_EVENT_AGE_SECONDS='300'
export TG_RUN_MAX_SECONDS="${TG_RUN_MAX_SECONDS:-90}"
export TG_AUTO_REPLY_PERSISTENT='1'
export TG_AUTO_REPLY_RECONCILE_SECONDS="${TG_AUTO_REPLY_RECONCILE_SECONDS:-30}"
export TG_LOG_SKIPS="${TG_LOG_SKIPS:-0}"
export TG_LOG_IDLE="${TG_LOG_IDLE:-0}"
export PYTHONUNBUFFERED='1'

xiu_ensure_session_authorized
exec ./.venv/bin/python3 -u auto_reply_topic_event.py
