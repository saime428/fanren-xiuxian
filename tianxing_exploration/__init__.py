"""Receipt-driven exploration for the explicitly configured Tianxing identity."""

from miniapp_shared.accounts import ACCOUNT_USERNAMES, accounts_for

VERSION = "tianxing-exploration-v1"
# The one account enabling tianxing_exploration; it explores as its main soul.
ACCOUNT = next(iter(accounts_for("tianxing_exploration")), None)
PLAYER_ID = int(ACCOUNT[2:]) if ACCOUNT else 0
USERNAME = ACCOUNT_USERNAMES.get(ACCOUNT, "")
FLOW = "tianxing_exploration"
