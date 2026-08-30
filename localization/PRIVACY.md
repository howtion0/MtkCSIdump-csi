# CSI capture privacy policy

CSI is not harmless telemetry. A capture carries transmitter MAC addresses,
fine-grained radio-channel changes correlated with presence and motion, host
timestamps, and room/device calibration metadata. Treat it as sensitive sensor
data even when packet payloads are absent.

The recorder therefore fails closed:

- the UDP source **IP and source port** and the one permitted transmitter
  address must be explicitly allowlisted;
- one session accepts one TA and one unchanged radio/tone configuration;
- capture/manifest/partial paths must be distinct; every path component is
  opened with no-symlink directory descriptors, unsafe writable ancestors are
  rejected, and the user-visible name is re-bound to the sealed inode before
  success; files are exclusive `0600`, flushed, and never overwrite a target;
- a successful file is length-framed, hashed, and sealed by a manifest;
- a failed handshake/capture leaves neither a final capture nor a manifest;
- manifests record router/interface, boot/radio epoch, code hashes, timebase,
  clock uncertainty, loss counters, channel/BW/tone mode, and time window.

Collect only on equipment and premises you are authorized to monitor. Inform
people in the monitored space, minimize duration and retention, and never
commit real `*.csi2`, `*.csi2f`, room maps, session manifests, or calibration
artifacts to a public repository. The default `.gitignore` enforces that policy;
only the deterministic encoder fixture and exact files marked
`SYNTHETIC SIMULATION — NOT HARDWARE EVIDENCE` are allowlisted. Artifact
directories are deny-by-default: adding a new file there does not make it
public. The source distribution is independently compared with the exact
`MANIFEST.in` allowlist and rejects extra, duplicate, symlink, hardlink, or
path-traversal members.

Content hashes detect accidental/tampered bytes; they do not authenticate the
operator claims inside a manifest. Receiver location, room label, timebase,
clock uncertainty, antenna mapping, known calibration angles, and training
source IDs must be supported by separate measurement records. Do not publish
private keys, router backups, Factory/NVRAM/calibration partitions, credentials,
or a real CSI capture even if a hash is attached.

Before sharing derived results, remove MAC addresses and exact timestamps,
publish failure cases and domain-shift tests, and keep the evidence flags. A
decorative “energy cloud” must never be presented as an observed body outline.
