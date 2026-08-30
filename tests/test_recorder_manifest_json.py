from __future__ import annotations

import io
import json
import os
import shutil
import stat
import struct
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from localization.contracts import validate_analysis_record
from localization.jsonio import dump_json, loads_json
from localization.recorder import (
    CSI2CaptureRecorder,
    RecorderMetadata,
    record_udp_session,
)
from localization.session import SequenceStats, SessionManifest, sha256_file
from tests.helpers import make_datagram


def _recorder_metadata() -> RecorderMetadata:
    return RecorderMetadata(
        session_id="s",
        receiver_id="r",
        router_model="Xiaomi AX3000T",
        interface="phy0-ap0",
        boot_id="b",
        radio_epoch="e",
        timebase_id="t",
        clock_uncertainty_ns=1,
        driver_commit="1dec35f",
        source_tree_hash="0" * 64,
    )


class _OnePacketSocket:
    def __init__(self) -> None:
        self.packet_delivered = False

    def bind(self, _address):
        return None

    def settimeout(self, _timeout):
        return None

    def sendto(self, payload, address):
        assert payload == b"register-v2"
        assert address == ("192.0.2.1", 8888)
        return len(payload)

    def recvfrom(self, _maximum):
        assert not self.packet_delivered
        self.packet_delivered = True
        return make_datagram(), ("192.0.2.1", 8888)

    def close(self):
        return None


def _record_one_packet(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "localization.recorder.socket.socket", lambda *_: _OnePacketSocket()
    )
    capture = tmp_path / "capture.csi2f"
    manifest = tmp_path / "session.json"
    result = record_udp_session(
        router_host="192.0.2.1",
        router_port=8888,
        listen_host="127.0.0.1",
        listen_port=8888,
        capture_path=capture,
        manifest_path=manifest,
        metadata=_recorder_metadata(),
        sender_allowlist={("192.0.2.1", 8888)},
        transmitter_allowlist={"02:11:22:33:44:55"},
        duration_s=1,
        maximum_packets=1,
    )
    return capture, manifest, result


def _manifest_for_capture(path: Path, recorder: CSI2CaptureRecorder) -> SessionManifest:
    assert recorder.radio_config is not None
    assert recorder.transmitter_address is not None
    return SessionManifest(
        session_id="test-session",
        receiver_id="receiver-a",
        router_model="Xiaomi AX3000T",
        interface="phy0-ap0",
        boot_id="boot-a",
        radio_epoch="radio-epoch-a",
        timebase_id="host-a-monotonic-mapping-v1",
        clock_uncertainty_ns=10_000,
        driver_commit="1dec35f376944e62bf826256cd1882b9c89080ce",
        source_tree_hash="1" * 64,
        capture_file=path.name,
        capture_sha256=sha256_file(path),
        start_host_timestamp_ns=int(recorder.start_ns),
        end_host_timestamp_ns=int(recorder.end_ns),
        radio_config=recorder.radio_config,
        sender_allowlist=("192.0.2.1:8888",),
        transmitter_allowlist=(recorder.transmitter_address,),
        sequence_stats=recorder.sequence_stats,
    )


def test_recorder_checks_source_endpoint_frames_and_counts_sequence_loss(
    tmp_path,
) -> None:
    output = io.BytesIO()
    recorder = CSI2CaptureRecorder(
        output,
        sender_allowlist={("192.0.2.1", 8888)},
        transmitter_allowlist={"02:11:22:33:44:55"},
    )
    with pytest.raises(ValueError, match="source-port"):
        recorder.ingest(("192.0.2.1", 9999), make_datagram(sequence=10))
    recorder.ingest(("192.0.2.1", 8888), make_datagram(sequence=10))
    recorder.ingest(("192.0.2.1", 8888), make_datagram(sequence=12))
    assert recorder.ingest(("192.0.2.1", 8888), make_datagram(sequence=12)) is None
    assert recorder.sequence_stats.accepted_datagrams == 2
    assert recorder.sequence_stats.estimated_lost_datagrams == 1
    assert recorder.sequence_stats.duplicate_datagrams == 1
    payload = output.getvalue()
    first_length = struct.unpack(">I", payload[:4])[0]
    assert first_length == len(make_datagram(sequence=10))

    capture = tmp_path / "authorized.synthetic.csi2f"
    capture.write_bytes(payload)
    manifest = _manifest_for_capture(capture, recorder)
    records = manifest.verify_capture(capture)
    assert [record.sequence for record in records] == [10, 12]
    assert validate_analysis_record(records[0]).sample_count == 64
    manifest.assert_records_verified(records)
    altered = [replace(records[0], samples=records[0].samples * 2), records[1]]
    with pytest.raises(ValueError, match="exact subset"):
        manifest.assert_records_verified(altered)
    freshly_loaded = SessionManifest.from_dict(manifest.to_dict())
    with pytest.raises(ValueError, match="bytes were not verified"):
        freshly_loaded.assert_records_verified(records)
    freshly_loaded.assert_records_verified(records, capture_path=capture)
    capture.write_bytes(payload + b"x")
    with pytest.raises(ValueError, match="SHA-256"):
        manifest.verify_capture(capture)
    with pytest.raises(ValueError, match="bytes were not verified"):
        manifest.assert_records_verified(records)


def test_recorder_rejects_rx_mode_tone_profile_change() -> None:
    recorder = CSI2CaptureRecorder(
        io.BytesIO(),
        sender_allowlist={("192.0.2.1", 8888)},
        transmitter_allowlist={"02:11:22:33:44:55"},
    )
    recorder.ingest(("192.0.2.1", 8888), make_datagram(sequence=10, rx_mode=4))
    with pytest.raises(ValueError, match="radio/tone configuration changed"):
        recorder.ingest(("192.0.2.1", 8888), make_datagram(sequence=11, rx_mode=8))


def test_manifest_loaded_fields_are_strictly_validated(tmp_path) -> None:
    output = io.BytesIO()
    recorder = CSI2CaptureRecorder(
        output,
        sender_allowlist={("192.0.2.1", 8888)},
        transmitter_allowlist={"02:11:22:33:44:55"},
    )
    recorder.ingest(("192.0.2.1", 8888), make_datagram())
    capture = tmp_path / "one.csi2f"
    capture.write_bytes(output.getvalue())
    manifest = _manifest_for_capture(capture, recorder)
    with pytest.raises(ValueError, match="safe basename"):
        replace(manifest, capture_file="../one.csi2f")
    with pytest.raises(ValueError, match="sender_allowlist"):
        replace(manifest, sender_allowlist=())
    with pytest.raises(ValueError, match="exactly one transmitter"):
        replace(
            manifest,
            transmitter_allowlist=(
                "02:11:22:33:44:55",
                "02:11:22:33:44:66",
            ),
        )
    with pytest.raises(ValueError, match="non-negative"):
        SequenceStats(accepted_datagrams=-1)
    malformed = manifest.to_dict()
    malformed["udp"]["sequence_stats"]["accepted_datagrams"] = 1.5
    with pytest.raises(ValueError, match="JSON integer"):
        SessionManifest.from_dict(malformed)


def test_record_session_handshake_failure_is_fail_closed(tmp_path, monkeypatch) -> None:
    class BrokenSocket:
        def bind(self, _address):
            return None

        def settimeout(self, _timeout):
            return None

        def sendto(self, _payload, _address):
            raise OSError("synthetic handshake failure")

        def close(self):
            return None

    monkeypatch.setattr(
        "localization.recorder.socket.socket", lambda *_: BrokenSocket()
    )
    capture = tmp_path / "capture.csi2f"
    manifest = tmp_path / "session.json"
    with pytest.raises(OSError, match="handshake"):
        record_udp_session(
            router_host="192.0.2.1",
            router_port=8888,
            listen_host="127.0.0.1",
            listen_port=8888,
            capture_path=capture,
            manifest_path=manifest,
            metadata=_recorder_metadata(),
            sender_allowlist={("192.0.2.1", 8888)},
            transmitter_allowlist={"02:11:22:33:44:55"},
            duration_s=1,
            maximum_packets=1,
        )
    assert not capture.exists()
    assert not manifest.exists()
    assert not Path(str(capture) + ".partial").exists()


def test_record_session_seals_private_files_without_overwrite(
    tmp_path, monkeypatch
) -> None:
    capture, manifest, result = _record_one_packet(tmp_path, monkeypatch)
    assert stat.S_IMODE(capture.stat().st_mode) == 0o600
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    assert not Path(str(capture) + ".partial").exists()
    assert not Path(str(manifest) + ".partial").exists()
    assert SessionManifest.load(manifest).computed_artifact_id() == (
        result.computed_artifact_id()
    )
    assert [record.sequence for record in result.verify_capture(capture)] == [77]


def test_verify_capture_binds_one_nofollow_descriptor_during_path_swap(
    tmp_path, monkeypatch
) -> None:
    capture, _manifest, result = _record_one_packet(tmp_path, monkeypatch)
    replacement = tmp_path / "attacker.csi2f"
    replacement.write_bytes(b"attacker-substitute")
    real_open = os.open
    swapped = False

    def open_then_swap(path, flags):
        nonlocal swapped
        descriptor = real_open(path, flags)
        if Path(path) == capture and not swapped:
            os.replace(replacement, capture)
            swapped = True
        return descriptor

    monkeypatch.setattr("localization.session.os.open", open_then_swap)
    records = result.verify_capture(capture)
    assert swapped
    assert capture.read_bytes() == b"attacker-substitute"
    result.assert_records_verified(records)
    with pytest.raises(ValueError, match="SHA-256"):
        result.assert_records_verified(records, capture_path=capture)
    with pytest.raises(ValueError, match="bytes were not verified"):
        result.assert_records_verified(records)


def test_record_session_rejects_colliding_paths_before_opening_socket(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "localization.recorder.socket.socket",
        lambda *_: pytest.fail("socket must not be opened for colliding paths"),
    )
    same = tmp_path / "same.csi2f"
    with pytest.raises(ValueError, match="must be distinct"):
        record_udp_session(
            router_host="192.0.2.1",
            router_port=8888,
            listen_host="127.0.0.1",
            listen_port=8888,
            capture_path=same,
            manifest_path=same,
            metadata=_recorder_metadata(),
            sender_allowlist={("192.0.2.1", 8888)},
            transmitter_allowlist={"02:11:22:33:44:55"},
            duration_s=1,
            maximum_packets=1,
        )


def test_record_session_preserves_preexisting_dangling_symlink(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "localization.recorder.socket.socket",
        lambda *_: pytest.fail("socket must not be opened for existing targets"),
    )
    capture = tmp_path / "capture.csi2f"
    capture_partial = Path(str(capture) + ".partial")
    os.symlink(tmp_path / "missing-private-target", capture_partial)
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        record_udp_session(
            router_host="192.0.2.1",
            router_port=8888,
            listen_host="127.0.0.1",
            listen_port=8888,
            capture_path=capture,
            manifest_path=tmp_path / "session.json",
            metadata=_recorder_metadata(),
            sender_allowlist={("192.0.2.1", 8888)},
            transmitter_allowlist={"02:11:22:33:44:55"},
            duration_s=1,
            maximum_packets=1,
        )
    assert capture_partial.is_symlink()


def test_capture_link_race_does_not_delete_competing_destination(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "localization.recorder.socket.socket", lambda *_: _OnePacketSocket()
    )
    capture = tmp_path / "capture.csi2f"
    original_link = os.link

    def racing_link(source, destination, **kwargs):
        if destination == capture.name:
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=kwargs["dst_dir_fd"],
            )
            try:
                os.write(descriptor, b"competing-owner")
            finally:
                os.close(descriptor)
        return original_link(source, destination, **kwargs)

    monkeypatch.setattr("localization.recorder.os.link", racing_link)
    with pytest.raises(FileExistsError):
        record_udp_session(
            router_host="192.0.2.1",
            router_port=8888,
            listen_host="127.0.0.1",
            listen_port=8888,
            capture_path=capture,
            manifest_path=tmp_path / "session.json",
            metadata=_recorder_metadata(),
            sender_allowlist={("192.0.2.1", 8888)},
            transmitter_allowlist={"02:11:22:33:44:55"},
            duration_s=1,
            maximum_packets=1,
        )
    assert capture.read_bytes() == b"competing-owner"


def test_record_session_rejects_symlinked_output_ancestor_before_socket(
    tmp_path, monkeypatch
) -> None:
    real_parent = tmp_path / "real-private"
    real_parent.mkdir(mode=0o700)
    linked_parent = tmp_path / "linked-private"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    monkeypatch.setattr(
        "localization.recorder.socket.socket",
        lambda *_: pytest.fail("socket must not open through a symlink ancestor"),
    )
    with pytest.raises(OSError):
        record_udp_session(
            router_host="192.0.2.1",
            router_port=8888,
            listen_host="127.0.0.1",
            listen_port=8888,
            capture_path=linked_parent / "capture.csi2f",
            manifest_path=linked_parent / "session.json",
            metadata=_recorder_metadata(),
            sender_allowlist={("192.0.2.1", 8888)},
            transmitter_allowlist={"02:11:22:33:44:55"},
            duration_s=1,
            maximum_packets=1,
        )
    assert not (real_parent / "capture.csi2f").exists()


def test_parent_swap_cannot_publish_attacker_bytes_as_success(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "localization.recorder.socket.socket", lambda *_: _OnePacketSocket()
    )
    output_parent = tmp_path / "private-output"
    output_parent.mkdir(mode=0o700)
    displaced_parent = tmp_path / "displaced-output"
    capture = output_parent / "capture.csi2f"
    manifest = output_parent / "session.json"
    original_link = os.link
    swapped = False

    def swapping_link(source, destination, **kwargs):
        nonlocal swapped
        result = original_link(source, destination, **kwargs)
        if destination == capture.name and not swapped:
            swapped = True
            output_parent.rename(displaced_parent)
            output_parent.mkdir(mode=0o700)
            (output_parent / capture.name).write_bytes(b"attacker-substitute")
        return result

    monkeypatch.setattr("localization.recorder.os.link", swapping_link)
    with pytest.raises(RuntimeError, match="parent changed"):
        record_udp_session(
            router_host="192.0.2.1",
            router_port=8888,
            listen_host="127.0.0.1",
            listen_port=8888,
            capture_path=capture,
            manifest_path=manifest,
            metadata=_recorder_metadata(),
            sender_allowlist={("192.0.2.1", 8888)},
            transmitter_allowlist={"02:11:22:33:44:55"},
            duration_s=1,
            maximum_packets=1,
        )
    assert capture.read_bytes() == b"attacker-substitute"
    assert not (displaced_parent / "capture.csi2f").exists()
    assert not (displaced_parent / "session.json").exists()


def test_all_json_is_standard_and_node_parseable(tmp_path) -> None:
    path = tmp_path / "strict.json"
    dump_json(path, {"infinite": float("inf"), "nan": float("nan"), "ok": 1.0})
    text = path.read_text()
    assert "Infinity" not in text and "NaN" not in text
    assert json.loads(text) == {"infinite": None, "nan": None, "ok": 1.0}
    with pytest.raises(ValueError, match="non-standard JSON"):
        loads_json('{"forbidden": NaN}')
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable")
    subprocess.run(
        [
            node,
            "-e",
            "JSON.parse(require('fs').readFileSync(process.argv[1],'utf8'))",
            str(path),
        ],
        check=True,
    )
