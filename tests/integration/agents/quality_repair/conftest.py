# -*- coding: utf-8 -*-
"""本次显式真实验收失败时留下脱敏停止记录；离线测试不写活动审计。"""

import os

import pytest

from tests.support.paths import REPO_ROOT
from tests.support.real_model_acceptance import record_failed_case


##### 验收失败停止板块 #####


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    result = outcome.get_result()
    if (result.failed and item.get_closest_marker('real_llm')
            and os.environ.get('SCHOLAR_RUN_UNIFIED_ACCEPTANCE') == '1'):
        record_failed_case(REPO_ROOT / 'tests/artifacts/AgentUnifiedRefactor/real_calls.json',
                           item.name)
