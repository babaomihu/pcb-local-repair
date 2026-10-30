# Exact local-middle protection mechanism (CC0-1.0)

Authored synthetic geometry by this project. The input has one unfinished ordinary signal; the candidate connects it and manually replaces a long neighbouring track with exact original outside tails and an internal jog. This tests native acceptance and is **not an automatic routing success or a real-board success**. The tampered candidate moves one original outside-tail endpoint by 1 nm and must be rejected.

From the package root with the native runtime configured:

```bash
pcb-local verify --config runtime.json --board examples/local-middle-native/input/board.kicad_pcb --roi 4 9 26 18 --layer F.Cu --net REPAIR_SIGNAL --signal-kind ordinary-single-ended --local-track 602adc27-c63d-527d-9d39-4989a20f4f69 --candidate examples/local-middle-native/candidate/board.kicad_pcb --out-dir proof-accepted
pcb-local verify --config runtime.json --board examples/local-middle-native/input/board.kicad_pcb --roi 4 9 26 18 --layer F.Cu --net REPAIR_SIGNAL --signal-kind ordinary-single-ended --local-track 602adc27-c63d-527d-9d39-4989a20f4f69 --candidate examples/local-middle-native/tampered/board.kicad_pcb --out-dir proof-rejected
```

Expected: accepted (21 native/protection checks), then protection_violation. Use new output folders when repeating. No user project is overwritten. These fixture files are dedicated to the public domain under CC0-1.0: https://creativecommons.org/publicdomain/zero/1.0/.
