#!/usr/bin/python3

import datetime
import pathlib

import pytest
from moonarchive.util.paths import (
    OutputPathTemplate,
    OutputPathTemplateCompat,
    OutputPathTemplateVars,
)

SAMPLE_VARS = OutputPathTemplateVars(
    title="【 PEAK 】4人で協力して山を登るゲーム？！やってみる！！【音乃瀬奏視点】#hololiveDEV_IS #ReGLOSS",
    id="NpGLZlDMHZs",
    video_id="NpGLZlDMHZs",
    channel_id="UCWQtYtq9EOB4-I5P-3fh8lA",
    channel="Kanade Ch. 音乃瀬奏 ‐ ReGLOSS",
    _start_datetime=datetime.datetime.fromisoformat("2025-08-25T12:03:33Z"),
)


@pytest.mark.parametrize(
    "input, expected",
    [
        (
            "%(title)s-%(id)s",
            "【 PEAK 】4人で協力して山を登るゲーム？！やってみる！！【音乃瀬奏視点】#hololiveDEV_IS #ReGLOSS-NpGLZlDMHZs",
        ),
    ],
)
def test_template_subsitutions(input: str, expected: str):
    template = OutputPathTemplateCompat(input)
    for suffix in (".mp4", ".description"):
        assert template.to_path(SAMPLE_VARS, suffix) == pathlib.Path(expected + suffix)
