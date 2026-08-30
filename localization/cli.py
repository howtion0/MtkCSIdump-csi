"""CLI for validated CSI2 capture, diagnostics, and synthetic demonstration."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

import numpy as np

from .aoa import estimate_coarse_aoa
from .calibration import ChainCalibration, ChainMapping, estimate_chain_calibration
from .cir import relative_cir_from_records
from .contracts import AnalysisContractError, validate_analysis_record
from .csi2 import decode_csi2_datagram, iter_length_prefixed_csi2
from .fusion import APObservation, fuse_grid_support
from .grouping import group_same_ppdu
from .jsonio import dump_json, dumps_json, load_json
from .range_proxy import (
    KNNRangeProxy,
    extract_bound_range_features,
    extract_range_features,
    feature_vector,
)
from .recorder import RecorderMetadata, record_udp_session
from .session import SessionManifest
from .simulate import (
    SYNTHETIC_NOTICE,
    default_mapping,
    simulate_strict_groups,
    synthetic_hardware_ratio,
    synthetic_session_manifest,
)
from .visualization import render_synthetic_room_svg


def _load_records(path: Path, framed: bool) -> list:
    with path.open("rb") as handle:
        if framed:
            return list(iter_length_prefixed_csi2(handle))
        return [decode_csi2_datagram(handle.read())]


def _load_verified_session(path: Path, capture: Path) -> SessionManifest:
    manifest = SessionManifest.load(path)
    manifest.verify_capture(capture)
    return manifest


def command_inspect(args: argparse.Namespace) -> int:
    records = _load_records(Path(args.capture), args.framed)
    groups = group_same_ppdu(records)
    contract_errors: list[str] = []
    for record in records:
        try:
            validate_analysis_record(record)
        except AnalysisContractError as exc:
            contract_errors.append(str(exc))
    summary = {
        "capture": str(Path(args.capture)),
        "record_count": len(records),
        "group_count": len(groups),
        "strict_group_count": sum(group.identity_mode == "strict" for group in groups),
        "phase_usable_group_count": sum(group.phase_usable for group in groups),
        "stream_keys": sorted(
            {
                (record.tx_idx, record.rx_idx, record.transport_stream)
                for record in records
            },
            key=str,
        ),
        "transmitters": sorted({record.transmitter_address for record in records}),
        "sample_counts": sorted({record.sample_count for record in records}),
        "quality_flags_union": int(
            np.bitwise_or.reduce([record.quality_flags for record in records])
        ),
        "analysis_contract_errors": sorted(set(contract_errors)),
        "flags": sorted({flag for group in groups for flag in group.evidence.flags}),
    }
    print(dumps_json(summary))
    return 0


def command_calibrate(args: argparse.Namespace) -> int:
    capture = Path(args.capture)
    records = _load_records(capture, True)
    manifest = _load_verified_session(Path(args.session), capture)
    manifest.assert_records_match(records)
    validation_capture = Path(args.validation_capture)
    validation_records = _load_records(validation_capture, True)
    validation_manifest = _load_verified_session(
        Path(args.validation_session), validation_capture
    )
    validation_manifest.assert_records_match(validation_records)
    groups = group_same_ppdu(records)
    validation_groups = group_same_ppdu(validation_records)
    mapping_data = load_json(args.mapping)
    mapping = ChainMapping.from_dict(mapping_data.get("mapping", mapping_data))
    calibration = estimate_chain_calibration(
        groups,
        mapping,
        session_manifest=manifest,
        known_angle_deg=args.known_angle,
        validation_groups=validation_groups,
        validation_session_manifest=validation_manifest,
        validation_angle_deg=args.validation_angle,
        minimum_packets=args.minimum_packets,
        minimum_valid_tone_fraction=args.minimum_valid_tone_fraction,
    )
    calibration.save(args.output)
    print(f"saved receiver/epoch-bound calibration to {args.output}")
    return 0


def command_aoa(args: argparse.Namespace) -> int:
    capture = Path(args.capture)
    records = _load_records(capture, True)
    manifest = _load_verified_session(Path(args.session), capture)
    groups = group_same_ppdu(records)
    calibration = ChainCalibration.load(args.calibration)
    result = estimate_coarse_aoa(
        groups,
        calibration,
        capture_manifest=manifest,
        sector_count=args.sectors,
        allow_low_confidence_pairing=args.allow_low_confidence,
        minimum_packets=args.minimum_packets,
    )
    payload = result.to_dict(include_spectrum=args.include_spectrum)
    if args.output:
        dump_json(Path(args.output), payload)
    else:
        print(dumps_json(payload))
    return 0


def command_cir(args: argparse.Namespace) -> int:
    capture = Path(args.capture)
    records = _load_records(capture, True)
    manifest = _load_verified_session(Path(args.session), capture)
    result = relative_cir_from_records(
        records,
        session_manifest=manifest,
        rx_idx=args.rx_idx,
        tx_idx=args.tx_idx,
        transport_stream=args.transport_stream,
        minimum_packets=args.minimum_packets,
        subcarrier_spacing_hz=args.subcarrier_spacing,
    )
    print(dumps_json(result.to_dict(include_profile=args.include_profile)))
    return 0


def command_record(args: argparse.Namespace) -> int:
    sender_endpoints: set[tuple[str, int]] = set()
    for value in args.allow_sender:
        try:
            host, port_text = value.rsplit(":", 1)
            sender_endpoints.add((host, int(port_text)))
        except (ValueError, TypeError) as exc:
            raise ValueError("--allow-sender must be IP:source-port") from exc
    metadata = RecorderMetadata(
        session_id=args.session_id,
        receiver_id=args.receiver_id,
        router_model=args.router_model,
        interface=args.interface,
        boot_id=args.boot_id,
        radio_epoch=args.radio_epoch,
        timebase_id=args.timebase_id,
        clock_uncertainty_ns=args.clock_uncertainty_ns,
        driver_commit=args.driver_commit,
        source_tree_hash=args.source_tree_hash,
    )
    manifest = record_udp_session(
        router_host=args.router_host,
        router_port=args.router_port,
        listen_host=args.listen_host,
        listen_port=args.listen_port,
        capture_path=args.capture,
        manifest_path=args.session,
        metadata=metadata,
        sender_allowlist=sender_endpoints,
        transmitter_allowlist=set(args.allow_ta),
        duration_s=args.duration,
        maximum_packets=args.maximum_packets,
    )
    print(dumps_json(manifest.to_dict()))
    return 0


def command_demo(args: argparse.Namespace) -> int:
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    hardware = synthetic_hardware_ratio(64)
    true_position = (3.8, 2.8)
    ap_specs = [
        ("SYNTHETIC-AX3000T-A", (0.7, 0.7), 0.0, 34.1, 4.0, 52),
        ("SYNTHETIC-AX3000T-B", (5.3, 0.8), 180.0, -53.1, 2.5, 53),
    ]
    range_training_mapping = default_mapping(receiver_id="SYNTHETIC-RANGE-TRAINER")
    training_rows: list[np.ndarray] = []
    training_labels: list[str] = []
    for class_index, (label, distance_m) in enumerate(
        (("near", 0.9), ("mid", 2.4), ("far", 4.8))
    ):
        for sample_index in range(18):
            training_groups = simulate_strict_groups(
                angle_deg=-40.0 + (sample_index % 9) * 10.0,
                packet_count=12,
                mapping=range_training_mapping,
                hardware_ratio=hardware,
                seed=1_000 + class_index * 100 + sample_index,
                distance_m=distance_m,
            )
            training_rows.append(
                feature_vector(extract_range_features(training_groups))
            )
            training_labels.append(label)
    training_x = np.vstack(training_rows)
    training_y = np.asarray(training_labels)
    training_source_id = (
        "sha256:"
        + hashlib.sha256(
            dumps_json(
                {
                    "notice": SYNTHETIC_NOTICE,
                    "features": training_x.tolist(),
                    "labels": training_y.tolist(),
                }
            ).encode("utf-8")
        ).hexdigest()
    )
    range_model = KNNRangeProxy.fit(
        training_x,
        training_y,
        room_id="synthetic-room",
        training_device_ids=["02:00:00:00:00:01"],
        training_source_ids=[training_source_id],
        k=7,
        synthetic=True,
        notes=SYNTHETIC_NOTICE,
    )
    range_model_path = output / "synthetic_range_proxy.json"
    range_model.save(range_model_path)
    observations: list[APObservation] = []
    payloads: dict[str, object] = {}
    for receiver_id, position, heading, angle, distance, seed in ap_specs:
        mapping = default_mapping(
            spacing_m=args.spacing,
            receiver_id=receiver_id,
            broadside_heading_deg=heading,
        )
        calibration_groups = simulate_strict_groups(
            angle_deg=25.0,
            packet_count=36,
            mapping=mapping,
            hardware_ratio=hardware,
            seed=seed - 10,
        )
        calibration_manifest = synthetic_session_manifest(
            calibration_groups,
            receiver_id=receiver_id,
            session_id=f"{receiver_id}-calibration",
        )
        validation_groups = simulate_strict_groups(
            angle_deg=-25.0,
            packet_count=36,
            mapping=mapping,
            hardware_ratio=hardware,
            seed=seed - 20,
        )
        validation_manifest = synthetic_session_manifest(
            validation_groups,
            receiver_id=receiver_id,
            session_id=f"{receiver_id}-validation",
            boot_id=calibration_manifest.boot_id,
            radio_epoch=calibration_manifest.radio_epoch,
        )
        calibration = estimate_chain_calibration(
            calibration_groups,
            mapping,
            session_manifest=calibration_manifest,
            known_angle_deg=25.0,
            validation_groups=validation_groups,
            validation_session_manifest=validation_manifest,
            validation_angle_deg=-25.0,
            minimum_packets=30,
            synthetic=True,
        )
        calibration_path = output / f"synthetic_calibration_{receiver_id[-1]}.json"
        calibration.save(calibration_path)

        target_groups = simulate_strict_groups(
            angle_deg=angle,
            packet_count=40,
            mapping=mapping,
            hardware_ratio=hardware,
            seed=seed,
            distance_m=distance,
        )
        target_manifest = synthetic_session_manifest(
            target_groups,
            receiver_id=receiver_id,
            session_id=f"{receiver_id}-target",
            boot_id=calibration_manifest.boot_id,
            radio_epoch=calibration_manifest.radio_epoch,
        )
        target_manifest_path = output / f"synthetic_session_{receiver_id[-1]}.json"
        target_manifest.save(target_manifest_path)
        aoa = estimate_coarse_aoa(
            target_groups,
            calibration,
            capture_manifest=target_manifest,
            sector_count=args.sectors,
        )
        bound_range_features = extract_bound_range_features(
            target_groups,
            session_manifest=target_manifest,
        )
        range_estimate = range_model.predict(
            bound_range_features.values,
            device_id="02:00:00:00:00:01",
            room_id="synthetic-room",
            feature_provenance=bound_range_features.provenance,
        )
        observations.append(
            APObservation.from_aoa(
                position,
                aoa,
                range_estimate=range_estimate,
            )
        )
        payloads[receiver_id] = {
            "aoa": aoa.to_dict(include_spectrum=False),
            "range_proxy": range_estimate.to_dict(),
            "session_manifest": target_manifest.to_dict(),
        }

    fused = fuse_grid_support(
        observations,
        room_bounds_m=(0.0, 6.0, 0.0, 4.0),
        room_id="synthetic-room",
        grid_step_m=0.08,
    )
    result = {
        "notice": SYNTHETIC_NOTICE,
        "truth_used_only_for_simulation": list(true_position),
        "per_receiver": payloads,
        "fused_support": fused.to_dict(include_grid=False),
    }
    result_path = output / "synthetic_result.json"
    dump_json(result_path, result)
    svg_path = output / "synthetic_room.svg"
    render_synthetic_room_svg(fused, observations, true_position, svg_path)
    print(SYNTHETIC_NOTICE)
    print(f"result: {result_path}")
    print(f"visual: {svg_path}")
    print(f"range model: {range_model_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ax3000t-localize",
        description="Evidence-gated AX3000T CSI2 coarse localization experiments",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("capture")
    inspect_parser.add_argument("--framed", action="store_true")
    inspect_parser.set_defaults(func=command_inspect)

    calibrate_parser = subparsers.add_parser("calibrate")
    calibrate_parser.add_argument("capture")
    calibrate_parser.add_argument("session")
    calibrate_parser.add_argument("validation_capture")
    calibrate_parser.add_argument("validation_session")
    calibrate_parser.add_argument("mapping")
    calibrate_parser.add_argument("output")
    calibrate_parser.add_argument("--known-angle", type=float, required=True)
    calibrate_parser.add_argument("--validation-angle", type=float, required=True)
    calibrate_parser.add_argument("--minimum-packets", type=int, default=30)
    calibrate_parser.add_argument(
        "--minimum-valid-tone-fraction", type=float, default=0.75
    )
    calibrate_parser.set_defaults(func=command_calibrate)

    aoa_parser = subparsers.add_parser("aoa")
    aoa_parser.add_argument("capture")
    aoa_parser.add_argument("session")
    aoa_parser.add_argument("calibration")
    aoa_parser.add_argument("--output")
    aoa_parser.add_argument("--sectors", type=int, default=13)
    aoa_parser.add_argument("--minimum-packets", type=int, default=12)
    aoa_parser.add_argument("--include-spectrum", action="store_true")
    aoa_parser.add_argument("--allow-low-confidence", action="store_true")
    aoa_parser.set_defaults(func=command_aoa)

    cir_parser = subparsers.add_parser("cir")
    cir_parser.add_argument("capture")
    cir_parser.add_argument("session")
    cir_parser.add_argument("--rx-idx", type=int, required=True)
    cir_parser.add_argument("--tx-idx", type=int)
    cir_parser.add_argument("--transport-stream", type=int)
    cir_parser.add_argument("--minimum-packets", type=int, default=12)
    cir_parser.add_argument("--subcarrier-spacing", type=float)
    cir_parser.add_argument("--include-profile", action="store_true")
    cir_parser.set_defaults(func=command_cir)

    record_parser = subparsers.add_parser("record")
    record_parser.add_argument("capture")
    record_parser.add_argument("session")
    record_parser.add_argument("--router-host", required=True)
    record_parser.add_argument("--router-port", type=int, default=8888)
    record_parser.add_argument("--listen-host", default="0.0.0.0")
    record_parser.add_argument("--listen-port", type=int, default=8888)
    record_parser.add_argument("--allow-sender", action="append", required=True)
    record_parser.add_argument("--allow-ta", action="append", required=True)
    record_parser.add_argument("--session-id", required=True)
    record_parser.add_argument("--receiver-id", required=True)
    record_parser.add_argument("--router-model", default="Xiaomi AX3000T")
    record_parser.add_argument("--interface", required=True)
    record_parser.add_argument("--boot-id", required=True)
    record_parser.add_argument("--radio-epoch", required=True)
    record_parser.add_argument("--timebase-id", required=True)
    record_parser.add_argument("--clock-uncertainty-ns", type=int, required=True)
    record_parser.add_argument("--driver-commit", required=True)
    record_parser.add_argument("--source-tree-hash", required=True)
    record_parser.add_argument("--duration", type=float, default=30.0)
    record_parser.add_argument("--maximum-packets", type=int, default=10_000)
    record_parser.set_defaults(func=command_record)

    demo_parser = subparsers.add_parser("demo")
    demo_parser.add_argument("--output-dir", default="synthetic-demo")
    demo_parser.add_argument("--spacing", type=float, default=0.028)
    demo_parser.add_argument("--sectors", type=int, default=17)
    demo_parser.set_defaults(func=command_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
