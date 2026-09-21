"""Tests for the AWS Solutions user-agent funnel.

WHAT IS AT STAKE: the string these tests pin is how AWS attributes this solution's
service API usage. Getting it wrong costs nothing at runtime - no call fails, no log
line appears - and is invisible until the attribution reports nothing. So the format,
the environment override and the survival of the string through a merge are all
asserted rather than assumed.

No account and no network: botocore builds a Config offline, which is the whole
mechanism under test. Whether a CLIENT then sends it is botocore's contract, not this
project's, and asserting it here would be asserting that botocore works.
"""

from __future__ import annotations

from botocore.config import Config

from corridor_event_hub.core.awsclients import (
    DEFAULT_USER_AGENT,
    SOLUTION_ID,
    SOLUTION_VERSION,
    USER_AGENT_ENV_VAR,
    solution_config,
    solution_user_agent,
)


class TestTheString:
    def test_the_format_is_the_one_onboarding_requires(self):
        # AWSSOLUTION/$solutionId/$solutionVersion, exactly.
        assert f"AWSSOLUTION/{SOLUTION_ID}/{SOLUTION_VERSION}" == DEFAULT_USER_AGENT
        assert DEFAULT_USER_AGENT == "AWSSOLUTION/SO0358/v1.0.0"

    def test_the_version_is_v_prefixed(self):
        # `1.0.0` would satisfy no reader of the attribution format.
        assert SOLUTION_VERSION.startswith("v")

    def test_the_environment_wins_over_the_compiled_in_default(self, monkeypatch):
        # The deployed template is authoritative: a Lambda reports the version it was
        # actually deployed from, not the one this package was built with.
        monkeypatch.setenv(USER_AGENT_ENV_VAR, "AWSSOLUTION/SO0358/v9.9.9")
        assert solution_user_agent() == "AWSSOLUTION/SO0358/v9.9.9"

    def test_the_default_is_used_when_nothing_is_set(self, monkeypatch):
        # The local tools (npm run trace, npm run probe) run with no CloudFormation
        # environment at all, and untagged calls from them would be a silent gap.
        monkeypatch.delenv(USER_AGENT_ENV_VAR, raising=False)
        assert solution_user_agent() == DEFAULT_USER_AGENT

    def test_an_empty_environment_variable_falls_back_rather_than_tagging_nothing(
        self, monkeypatch
    ):
        monkeypatch.setenv(USER_AGENT_ENV_VAR, "")
        assert solution_user_agent() == DEFAULT_USER_AGENT


class TestTheConfig:
    def test_it_lands_in_user_agent_extra(self, monkeypatch):
        monkeypatch.delenv(USER_AGENT_ENV_VAR, raising=False)
        assert solution_config().user_agent_extra == DEFAULT_USER_AGENT

    def test_a_callers_settings_survive_and_so_does_the_attribution(self, monkeypatch):
        # The likeliest future edit is someone adding a timeout or a retry policy. That
        # must not drop the user agent, which is why solution_config merges.
        monkeypatch.delenv(USER_AGENT_ENV_VAR, raising=False)
        merged = solution_config(Config(read_timeout=5, retries={"max_attempts": 2}))
        assert merged.read_timeout == 5
        assert merged.retries["max_attempts"] == 2
        assert merged.user_agent_extra == DEFAULT_USER_AGENT

    def test_a_callers_own_user_agent_extra_is_kept_alongside_ours(self, monkeypatch):
        # Both are things somebody deliberately asked to appear in the header, so
        # neither is dropped in favour of the other.
        monkeypatch.delenv(USER_AGENT_ENV_VAR, raising=False)
        merged = solution_config(Config(user_agent_extra="SomethingElse/1.0"))
        assert "SomethingElse/1.0" in merged.user_agent_extra
        assert DEFAULT_USER_AGENT in merged.user_agent_extra
