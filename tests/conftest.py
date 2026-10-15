import copy
import io

import pytest
from PIL import Image

from core.story import Message


@pytest.fixture
def messages():
    return [
        Message("m1", "u1", "甲", "这次服务器绝对稳定", "10001", 1),
        Message("m2", "u2", "乙", "已经打不开了", "10002", 2),
    ]


@pytest.fixture
def story():
    return copy.deepcopy(
        {
            "title": "稳定离线",
            "premise": "夸完服务器就断线",
            "adaptation": "最后一句为喜剧改编",
            "source_ids": ["m1", "m2"],
            "cast": [
                {"id": "u1", "appearance": "蓝色外套"},
                {"id": "u2", "appearance": "红色围巾"},
            ],
            "panels": [
                {
                    "beat": "铺垫",
                    "action": "甲自信举起电脑",
                    "dialogue": [{"speaker": "u1", "text": "这次绝对稳定"}],
                },
                {
                    "beat": "推进",
                    "action": "乙点击网页",
                    "dialogue": [{"speaker": "u2", "text": "我试试"}],
                },
                {
                    "beat": "转折",
                    "action": "网页显示离线",
                    "dialogue": [{"speaker": "u2", "text": "打不开呀"}],
                },
                {
                    "beat": "包袱",
                    "action": "甲擦汗嘴硬",
                    "dialogue": [{"speaker": "u1", "text": "稳定地离线了"}],
                },
            ],
        }
    )


@pytest.fixture
def png():
    b = io.BytesIO()
    Image.new("RGB", (32, 32), "red").save(b, format="PNG")
    return b.getvalue()
