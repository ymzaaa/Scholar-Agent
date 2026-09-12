# -*- coding: utf-8 -*-
"""不访问网络的可编程大语言模型测试客户端。"""

from types import SimpleNamespace


##### 假客户端板块 #####


class FakeLLMClient:
    """按预设响应返回结果，并记录实际调用次数。"""

    def __init__(self, responses=None, *, configured: bool = True) -> None:
        self.responses = list(responses or [])
        self.configured = configured
        self.calls = 0
        self.s = SimpleNamespace(llm_provider="test", llm_model="fake-model")
        self.endpoint_host = "fake.invalid"

    def complete_text(self, _system, user, **_kwargs):
        self.calls += 1
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        ids = []
        for line in user.splitlines():
            if line.startswith("ID="):
                ids.append(int(line.split(" ", 1)[0].split("=")[1]))
        return "\n".join(f"ID={index}: EN-{index}" for index in ids)
