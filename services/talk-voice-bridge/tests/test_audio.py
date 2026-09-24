import audio


def test_device_names():
    assert audio.SPEAKER_MONITOR == "talk_speaker.monitor"
    assert audio.MIC_SINK == "talk_mic_sink"


def test_pcm_b64_round_trip():
    pcm = b"\x01\x02\x03\x04"
    encoded = audio.pcm_to_b64(pcm)
    assert isinstance(encoded, str)
    assert audio.b64_to_pcm(encoded) == pcm


def test_parec_cmd_records_from_speaker_monitor():
    cmd = audio.parec_cmd(24000)
    assert isinstance(cmd, list)
    assert cmd[0] == "parec"
    assert f"--device={audio.SPEAKER_MONITOR}" in cmd
    assert "--format=s16le" in cmd
    assert "--rate=24000" in cmd
    assert "--channels=1" in cmd
    assert "--raw" in cmd


def test_parec_cmd_embeds_given_rate():
    cmd = audio.parec_cmd(48000)
    assert "--rate=48000" in cmd


def test_pacat_cmd_plays_into_mic_sink():
    cmd = audio.pacat_cmd(24000)
    assert isinstance(cmd, list)
    assert cmd[0] == "pacat"
    assert f"--device={audio.MIC_SINK}" in cmd
    assert "--format=s16le" in cmd
    assert "--rate=24000" in cmd
    assert "--channels=1" in cmd
    assert "--raw" in cmd
    assert "--playback" in cmd
