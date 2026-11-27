# 8 · Laboratory validation

Everything in this repository was tested against a **mock** OPC UA server built from the PLC
project (see [06-testing.md](06-testing.md#66-what-the-mock-cannot-prove)). The checks below must be
done **at the real factory** before anyone relies on the dashboard. Work through them in order,
write the results into the table at the end, and do not skip a failed step.

## 8.0 Safety rules (read first)

* Every step only **reads** from the plant. The dashboard and all tools in `backend/dmf/cli` use a
  client wrapper without any write, call or node-management function (`test_readonly_guard`).
* Do **not** use UaExpert's write, call or "Add/Delete node" functions during these checks. Browse
  and monitor only.
* Moving pieces on the line (steps 3–7) is done by the lab staff with the normal plant controls;
  the dashboard is a spectator.
* **Stop condition:** if any PLC program, plant value or plant behaviour changes in a way you did
  not cause by hand, stop the backend (`docker compose stop backend`) and inform the lab staff.

## 8.1 Read-only OPC UA account (server-side enforcement)

The backend never writes, but defence in depth requires that the server **refuses** writes from
the dashboard's account as well. Ask the lab's PLC administrator to set this up — the dashboard team
must not change PLC projects.

Important: in this plant the cell PLCs deliver their data **by writing into the central server**
(`OPC_Write_Cell`). Making the variables read-only for *everybody* (e.g. clearing "Writable from
HMI/OPC UA" on the data blocks) would stop the plant data. The restriction must apply **only to the
dashboard's user**:

1. In the server's user management (for an S7-1500 integrated server: TIA Portal → PLC properties →
   OPC UA → Server → Security → User authentication; with TIA Portal V17+ user management, a role),
   create a user, e.g. `dmf-reader`, with **read/browse/subscribe rights only** — no write, no
   method call. Disable anonymous ("guest") access for writing if the other clients allow it.
2. If the server offers secure endpoints, prefer `Basic256Sha256` with `Sign` or `SignAndEncrypt`
   and create a client certificate for the dashboard (`OPCUA_SECURITY`, see `.env.example`).
3. Put the credentials into `.env` (`OPCUA_USERNAME`, `OPCUA_PASSWORD`); they are never logged
   (the endpoint is shown with `***` in logs and `/api/status`).

| Expected observation | Acceptance criterion | If not met |
|---|---|---|
| `python -m dmf.cli.verify` ends with exit code **0** and prints `0 writable for this user` | every node's `UserAccessLevel` lacks the write bit for `dmf-reader` | exit code **3** = everything matches but the user may write: ask the administrator again; if no read-only user is possible, record this as an accepted risk in the results table (the dashboard still never writes) |

`verify` reads the `UserAccessLevel` attribute; it never tries a write to find out.

## 8.2 Step 1 · Reach the server

1. Connect the dashboard computer to the plant network: `ping 192.168.0.10`.
2. In UaExpert, add `opc.tcp://192.168.0.10:4840`, note the offered **security policies and user
   token types**, and connect with the dashboard's user.
3. In the namespace array (Server → NamespaceArray) look for `http://demo-plantV2`.

| Expected observation | Acceptance criterion | Failure handling |
|---|---|---|
| ping answers; UaExpert connects; namespace present | all three true | no ping: network/VLAN/firewall (lab IT). Connection refused: endpoint/port or security policy. Namespace missing or renamed: set the new URI in `config/opcua_nodes.yaml` (`namespace_uri`) — the index is resolved at runtime, not hard-coded |

## 8.3 Step 2 · Verify the node map

```bash
cd backend && . .venv/bin/activate          # or: docker compose exec backend ...
OPCUA_ENDPOINT=opc.tcp://192.168.0.10:4840 python -m dmf.cli.verify --json ../recordings/verify-report.json
```

| Expected observation | Acceptance criterion | Failure handling |
|---|---|---|
| `Checked N nodes: 0 missing, 0 with a different browse name, 0 values out of range, 0 writable` | exit code 0 | **missing / different browse name**: correct `config/opcua_nodes.yaml` (or fix the extractor in `tools/tia_nodemap` and regenerate) and run again. Until fixed, the backend ignores the values of mismatching nodes (cell shows *no valid data*). **Out of range**: the state/lamp meaning differs from the documentation — stop and clarify with the lab before continuing |

When everything matches, set `verification: verified` in `config/opcua_nodes.yaml`; the footer note
"node map not verified" in the UI then disappears.

## 8.4 Step 3 · Confirm the plant structure

Watch the line and UaExpert's monitored items; correct `config/plant.yaml` where needed.

| # | Check | How | Acceptance criterion |
|---|---|---|---|
| 3.1 | `ZelleN` is **position N** | Put a piece on a known cell; watch which `ZelleN.Fb*_Zustand` changes | the changing node belongs to that position for at least 3 different positions incl. a cross cell (`Zelle5`) |
| 3.2 | Cell numbers per position | Compare hardware labels with the map | all 18 match `cell:` (table in docs/01) |
| 3.3 | Port connections | Send pieces along every route (step 4) | each hand-over in `connections:` is observed as `4→0` on the source and `0→1→2` on the target within `HANDOVER_WINDOW_S` |
| 3.4 | Robot / manual transfers **11→12, 17→18 and 14→15** | Watch positions 11, 12, 14, 15, 17, 18 during transfers | 14→15: confirm there is **no** PLC hand-over (source `3→0` without `4`, no neighbour flags) — then the robot link is right; if `3→4→0` with a matching `0→1→2` is seen, move it back to `connections:`. For every robot link measure the longest transfer and set `max_transit_s` to about twice that |
| 3.5 | Storage sources and sinks | Watch where pieces appear and disappear | matches `external:` |
| 3.6 | State values 0–6 and lamp 1–4 | Watch while the line runs | turntable shows `4→6→0` after a piece leaves (state 6 = empty, decision I-1); lamp 2 while waiting at an exit, 3 idle, 4 transporting, 1 on error/not enabled |
| 3.7 | Sampling | Check the revised sampling interval reported by the server (UaExpert: monitored item properties) | ≤ 200 ms, so that the short state 4 is seen; otherwise hand-overs are paired by the fallback and marked *uncertain* |

## 8.5 Step 4 · Record real traffic and check the tracker

```bash
python -m dmf.cli.record --out ../recordings/lab-$(date +%F).jsonl --minutes 30
python -m dmf.cli.replay_check ../recordings/lab-$(date +%F).jsonl --events > ../recordings/lab-check.txt
```

While recording, note by hand at least 10 pieces: when they were put on, their route, and when they
were taken off — including one through each robot transfer, one into storage and two pieces running
close behind each other.

| Expected observation | Acceptance criterion | Failure handling |
|---|---|---|
| every noted piece numbered once, cell by cell, ending as *taken off* / *stored* | 10/10 noted pieces match; 0 `lost`; `found` only for pieces that really appeared mid-line; anomalies explained | `found`/`lost`: wrong connection, wrong node id or too short `HANDOVER_WINDOW_S`. Many *uncertain* "hand-over seen without the handing-over state": sampling too coarse (3.7). Number swaps at a robot: the robot reorders — document as a known limitation |

Keep one representative recording as regression data: copy it to `backend/tests/data/` and add it to
`tests/unit/test_replay_recording.py` with the summary you verified by hand.

## 8.6 Step 5 · Tune the timings

| Setting | Start value | Adjust when | Acceptance criterion |
|---|---|---|---|
| `HANDOVER_WINDOW_S` | 8 s | the longest hand-over seen in step 4 (source `4→0` to target `1→2`) is close to it | ≥ 2 × the longest observed hand-over |
| `STUCK_AFTER_S` | 120 s | normal waiting at exits takes longer (false "stuck") | no *stuck* during 30 min of normal production; a deliberately held piece becomes *stuck* |
| `robot_links[].max_transit_s` | 300 s | pieces are *lost* after a robot transfer and reappear with a new number | ≥ 2 × the longest observed transfer |

## 8.7 Step 6 · Offline detection (optional)

A PLC that is switched off may leave its **last values** on the server, and a subscription cannot
tell "idle" from "off". Check whether the server refreshes the **SourceTimestamp** of values the PLC
writes periodically, even when unchanged:

1. In UaExpert, watch `ZelleN.AmpelZustand` of an idle cell for two minutes: does the source
   timestamp advance? (The dashboard only compares *changes* of that timestamp, so a PLC clock that
   is off does not matter.)
2. If yes, set `OFFLINE_DETECTION=timestamps` and `OFFLINE_AFTER_S` to about 3× the refresh period,
   switch one cell off, and check it shows *Offline – no data* within `OFFLINE_AFTER_S +
   OFFLINE_CHECK_INTERVAL_S`, and *Running* again after switching it on.
3. If no, leave it `off`. Cells then show *Offline* only when the server is unreachable or reports
   Bad quality — record this limitation.

## 8.8 Step 7 · End-to-end acceptance on the plant

With the dashboard running against the plant (`docker compose up -d`):

| # | Action | Expected observation | Pass |
|---|---|---|---|
| 7.1 | line idle and enabled | all 18 cells *Running*; header "Live" | ☐ |
| 7.2 | put a piece on at a side entry | it gets the next number and appears in *Pieces now* at the right cell within 2 s | ☐ |
| 7.3 | let it run through 3+ cells | *History* journey lists every cell with plausible times (Berlin time) | ☐ |
| 7.4 | hold a piece at an exit for > `STUCK_AFTER_S` | *Piece stuck* on the map, under *Needs attention* and in *Pieces now*; after release the pass shows "Passed (was stuck)" | ☐ |
| 7.5 | emergency stop / cell error | that cell *Error*; after reset *Running*; a piece in transit at that moment keeps its number | ☐ |
| 7.6 | unplug the dashboard's network cable for 1 min | "No connection to the factory", last known state frozen and labelled; after plugging back: recovers by itself, pieces kept or marked *uncertain* | ☐ |
| 7.7 | `docker compose restart backend` while pieces run | numbering continues; pieces on the line keep their numbers (some may be *uncertain*) | ☐ |
| 7.8 | stop the database (`docker compose stop db`) for 1 min, then start it | live view keeps working, banner "events are waiting"; afterwards History contains the passes of that minute | ☐ |
| 7.9 | CSV and PDF export of today | open in Excel / a PDF viewer; times local; numbers match the History page | ☐ |
| 7.10 | after all tests | `python -m dmf.cli.verify` exit 0 (or 3 recorded as accepted risk); PLC programs unchanged (ask the lab to compare the project checksum) | ☐ |

**Go-live criterion:** steps 1–4 and 7.1–7.10 passed (step 6 optional), deviations corrected in the
configuration and re-tested, results signed in the table below.

## Results

| Step | Date | By | Result / changes made |
|---|---|---|---|
| 8.1 Read-only account | | | |
| 1 Reach the server | | | |
| 2 Node map | | | |
| 3 Structure (incl. 14→15) | | | |
| 4 Recording | | | |
| 5 Timings | | | |
| 6 Offline detection | | | |
| 7 Acceptance | | | |
