from pocketcoder_bot.access import AccessPolicy, is_allowed


def test_access_policy_disabled_allows_anyone() -> None:
    policy = AccessPolicy(allowed_user_ids=set(), allowed_chat_ids=set())
    assert policy.enabled is False
    assert is_allowed(policy, user_id=None, chat_id=None) is True
    assert is_allowed(policy, user_id=1, chat_id=2) is True


def test_access_policy_requires_allowed_user() -> None:
    policy = AccessPolicy(allowed_user_ids={100}, allowed_chat_ids=set())
    assert policy.enabled is True
    assert is_allowed(policy, user_id=100, chat_id=1) is True
    assert is_allowed(policy, user_id=200, chat_id=1) is False
    assert is_allowed(policy, user_id=None, chat_id=1) is False


def test_access_policy_requires_allowed_chat() -> None:
    policy = AccessPolicy(allowed_user_ids=set(), allowed_chat_ids={777})
    assert is_allowed(policy, user_id=100, chat_id=777) is True
    assert is_allowed(policy, user_id=100, chat_id=999) is False
    assert is_allowed(policy, user_id=100, chat_id=None) is False


def test_access_policy_combined_filters() -> None:
    policy = AccessPolicy(allowed_user_ids={7}, allowed_chat_ids={9})
    assert is_allowed(policy, user_id=7, chat_id=9) is True
    assert is_allowed(policy, user_id=7, chat_id=1) is False
    assert is_allowed(policy, user_id=1, chat_id=9) is False
