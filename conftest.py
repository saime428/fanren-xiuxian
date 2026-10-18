"""Tests always run against the tracked demo accounts, never a local accounts.json."""
import os

os.environ["XIUXIAN_EXAMPLE_CONFIG"] = "1"
os.environ.pop("XIUXIAN_ACCOUNTS_FILE", None)
