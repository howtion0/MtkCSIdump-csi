# Synthetic demonstration artifacts

**SYNTHETIC SIMULATION — NOT HARDWARE EVIDENCE**

These files are deterministic output from:

```bash
python3 -m localization.cli demo --output-dir synthetic-demo --sectors 17
```

- `synthetic_room.svg` is a presentation-oriented normalized-support heatmap.
- `synthetic_result.json` contains the injected truth and algorithm output.
- `synthetic_calibration_A/B.json` contain separate synthetic receiver RF-chain
  responses plus independent opposite-side validation manifests/residuals.
- `synthetic_session_A/B.json` contain synthetic capture provenance/timebase manifests.
- `synthetic_range_proxy.json` contains a synthetic labeled kNN model trained
  from features extracted from simulated CSI windows. Each fused prediction is
  bound to the matching synthetic target manifest/receiver/TA/time window.

They verify serialization, inference and rendering. They do not measure an
AX3000T, a phone, a room, localization accuracy, or human pose.
