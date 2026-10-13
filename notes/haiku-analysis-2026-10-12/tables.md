## All runs: role x model
| key | n | median tok/s | p25-p75 tok/s | median wall s | median turns | sum wall min | sum $ | $/agent | cache-read share of input | output tok |
|---|---|---|---|---|---|---|---|---|---|---|
| fix / opus | 1 | 53 | - | 1703 | 6 | 28.4 | 0.85 | 0.851 | 47.9% | 193 |
| fix / sonnet | 1 | 97 | - | 616 | 4 | 10.3 | 0.16 | 0.163 | 74.3% | 186 |
| gate / haiku | 7 | 115 | 69-121 | 70 | 5 | 45.4 | 0.08 | 0.011 | 70.8% | 1519 |
| gate / opus | 12 | 42 | 39-65 | 67 | 6 | 153.7 | 6.64 | 0.553 | 70.3% | 3022 |
| impl / haiku | 52 | 226 | 201-250 | 212 | 36 | 197.9 | 8.15 | 0.157 | 96.9% | 533100 |
| impl / opus | 5 | 112 | 99-115 | 179 | 31 | 18.4 | 5.87 | 1.173 | 96.2% | 50910 |
| impl / sonnet | 34 | 150 | 133-158 | 263 | 16 | 250.0 | 22.77 | 0.670 | 92.7% | 47860 |
| impl-resume / sonnet | 8 | 137 | 132-152 | 227 | 14 | 43.4 | 2.58 | 0.322 | 89.5% | 8659 |
| impl-retry / haiku | 9 | 230 | 225-252 | 67 | 12 | 14.7 | 0.20 | 0.022 | 93.1% | 19546 |
| impl-retry / opus | 7 | 120 | 116-122 | 171 | 14 | 25.2 | 5.27 | 0.753 | 94.2% | 34148 |
| impl-retry / sonnet | 15 | 142 | 134-147 | 21 | 5 | 10.7 | 2.68 | 0.179 | 81.4% | 17829 |
| impl-revise / haiku | 18 | 229 | 218-244 | 135 | 25 | 52.1 | 1.18 | 0.066 | 95.4% | 158533 |
| impl-revise / opus | 4 | 118 | 116-124 | 135 | 22 | 9.2 | 3.21 | 0.803 | 95.0% | 28199 |
| impl-revise / sonnet | 23 | 144 | 131-153 | 140 | 12 | 91.8 | 7.22 | 0.314 | 92.2% | 33052 |
| land / haiku | 58 | 194 | 155-210 | 29 | 4 | 41.1 | 0.43 | 0.007 | 83.0% | 27577 |
| land / opus | 56 | 67 | 65-70 | 53 | 3 | 183.7 | 15.36 | 0.274 | 75.2% | 10412 |
| orchestrator / haiku | 1 | 213 | - | 1362 | 11 | 22.7 | 0.05 | 0.048 | 89.1% | 11521 |
| orchestrator / opus | 9 | 102 | 101-104 | 5898 | 12 | 895.8 | 24.74 | 2.749 | 92.2% | 91656 |
| orchestrator / sonnet | 3 | 139 | - | 3221 | 7 | 173.3 | 1.59 | 0.529 | 78.1% | 21014 |
| plan / haiku | 193 | 210 | 192-226 | 6 | 2 | 21.1 | 1.14 | 0.006 | 62.3% | 77514 |
| plan / opus | 85 | 95 | 91-99 | 9 | 2 | 207.6 | 22.55 | 0.265 | 52.6% | 26920 |
| review / haiku | 17 | 213 | 209-220 | 67 | 10 | 21.4 | 0.53 | 0.031 | 90.1% | 72087 |
| review / opus | 145 | 102 | 99-105 | 135 | 14 | 576.6 | 100.63 | 0.694 | 92.7% | 527701 |
| review / sonnet | 84 | 125 | 119-130 | 67 | 9 | 100.8 | 33.44 | 0.398 | 88.9% | 506107 |
| test / haiku | 29 | 220 | 204-253 | 310 | 42 | 192.6 | 9.50 | 0.328 | 96.8% | 487340 |
| test / sonnet | 32 | 151 | 144-159 | 150 | 18 | 108.2 | 20.80 | 0.650 | 94.9% | 48156 |
| test-resume / sonnet | 2 | 148 | - | 59 | 12 | 2.0 | 0.55 | 0.276 | 90.8% | 1667 |
| test-retry / haiku | 2 | 199 | - | 56 | 14 | 1.9 | 0.03 | 0.017 | 92.4% | 3963 |
| test-retry / opus | 2 | 118 | - | 101 | 18 | 3.4 | 1.41 | 0.707 | 93.8% | 8420 |
| test-retry / sonnet | 4 | 137 | 131-145 | 17 | 4 | 1.2 | 0.68 | 0.171 | 78.7% | 4743 |
| test-revise / haiku | 20 | 235 | 215-254 | 116 | 24 | 77.1 | 2.13 | 0.106 | 95.2% | 160637 |
| test-revise / opus | 2 | 129 | - | 160 | 22 | 5.3 | 1.83 | 0.916 | 95.0% | 19222 |
| test-revise / sonnet | 35 | 148 | 141-155 | 74 | 11 | 75.0 | 10.37 | 0.296 | 92.0% | 45773 |

## Per run: role x model
| key | n | median tok/s | p25-p75 tok/s | median wall s | median turns | sum wall min | sum $ | $/agent | cache-read share of input | output tok |
|---|---|---|---|---|---|---|---|---|---|---|
| all-haiku (10-10) / gate / haiku | 1 | 69 | - | 70 | 5 | 1.2 | 0.01 | 0.008 | 79.2% | 73 |
| all-haiku (10-10) / impl / haiku | 11 | 249 | 226-259 | 144 | 29 | 28.8 | 1.05 | 0.096 | 96.1% | 20986 |
| all-haiku (10-10) / impl-retry / haiku | 1 | 243 | - | 67 | 29 | 1.1 | 0.03 | 0.030 | 96.0% | 2008 |
| all-haiku (10-10) / impl-revise / haiku | 3 | 222 | - | 100 | 22 | 4.9 | 0.08 | 0.028 | 94.9% | 5109 |
| all-haiku (10-10) / land / haiku | 9 | 155 | 151-165 | 14 | 6 | 2.8 | 0.08 | 0.009 | 82.9% | 2738 |
| all-haiku (10-10) / orchestrator / haiku | 1 | 213 | - | 1362 | 11 | 22.7 | 0.05 | 0.048 | 89.1% | 11521 |
| all-haiku (10-10) / plan / haiku | 32 | 218 | 206-226 | 4 | 2 | 2.7 | 0.21 | 0.006 | 59.9% | 14146 |
| all-haiku (10-10) / review / haiku | 17 | 213 | 209-220 | 67 | 10 | 21.4 | 0.53 | 0.031 | 90.1% | 72087 |
| all-haiku (10-10) / test / haiku | 2 | 217 | - | 476 | 48 | 15.9 | 0.60 | 0.302 | 97.2% | 4221 |
| all-haiku (10-10) / test-revise / haiku | 2 | 246 | - | 159 | 36 | 5.3 | 0.20 | 0.101 | 96.7% | 4489 |
| baseline sonnet+opus (10-10..12) / fix / opus | 1 | 53 | - | 1703 | 6 | 28.4 | 0.85 | 0.851 | 47.9% | 193 |
| baseline sonnet+opus (10-10..12) / fix / sonnet | 1 | 97 | - | 616 | 4 | 10.3 | 0.16 | 0.163 | 74.3% | 186 |
| baseline sonnet+opus (10-10..12) / gate / opus | 12 | 42 | 39-65 | 67 | 6 | 153.7 | 6.64 | 0.553 | 70.3% | 3022 |
| baseline sonnet+opus (10-10..12) / impl / sonnet | 34 | 150 | 133-158 | 263 | 16 | 250.0 | 22.77 | 0.670 | 92.7% | 47860 |
| baseline sonnet+opus (10-10..12) / impl-resume / sonnet | 8 | 137 | 132-152 | 227 | 14 | 43.4 | 2.58 | 0.322 | 89.5% | 8659 |
| baseline sonnet+opus (10-10..12) / impl-retry / sonnet | 8 | 144 | 139-150 | 10 | 3 | 5.5 | 1.31 | 0.163 | 78.8% | 6338 |
| baseline sonnet+opus (10-10..12) / impl-revise / sonnet | 18 | 145 | 135-154 | 276 | 13 | 85.7 | 5.79 | 0.322 | 92.4% | 21974 |
| baseline sonnet+opus (10-10..12) / land / opus | 56 | 67 | 65-70 | 53 | 3 | 183.7 | 15.36 | 0.274 | 75.2% | 10412 |
| baseline sonnet+opus (10-10..12) / orchestrator / opus | 9 | 102 | 101-104 | 5898 | 12 | 895.8 | 24.74 | 2.749 | 92.2% | 91656 |
| baseline sonnet+opus (10-10..12) / plan / opus | 85 | 95 | 91-99 | 9 | 2 | 207.6 | 22.55 | 0.265 | 52.6% | 26920 |
| baseline sonnet+opus (10-10..12) / review / opus | 112 | 101 | 98-103 | 154 | 14 | 511.2 | 77.37 | 0.691 | 92.9% | 332842 |
| baseline sonnet+opus (10-10..12) / test / sonnet | 32 | 151 | 144-159 | 150 | 18 | 108.2 | 20.80 | 0.650 | 94.9% | 48156 |
| baseline sonnet+opus (10-10..12) / test-resume / sonnet | 2 | 148 | - | 59 | 12 | 2.0 | 0.55 | 0.276 | 90.8% | 1667 |
| baseline sonnet+opus (10-10..12) / test-retry / sonnet | 2 | 144 | - | 10 | 4 | 0.3 | 0.29 | 0.144 | 74.9% | 1144 |
| baseline sonnet+opus (10-10..12) / test-revise / sonnet | 28 | 151 | 144-156 | 77 | 11 | 63.8 | 7.79 | 0.278 | 91.7% | 26934 |
| bench-final (ck0.9.0, 10-12) / gate / haiku | 2 | 113 | - | 1218 | 8 | 40.6 | 0.04 | 0.020 | 64.2% | 361 |
| bench-final (ck0.9.0, 10-12) / impl / haiku | 13 | 262 | 241-267 | 210 | 40 | 45.3 | 2.78 | 0.214 | 96.9% | 219419 |
| bench-final (ck0.9.0, 10-12) / impl / opus | 3 | 115 | - | 179 | 31 | 9.8 | 3.54 | 1.179 | 96.2% | 36713 |
| bench-final (ck0.9.0, 10-12) / impl-retry / haiku | 4 | 252 | - | 45 | 12 | 3.2 | 0.06 | 0.015 | 90.9% | 6172 |
| bench-final (ck0.9.0, 10-12) / impl-retry / opus | 4 | 121 | 120-128 | 149 | 14 | 15.8 | 3.37 | 0.843 | 93.5% | 22621 |
| bench-final (ck0.9.0, 10-12) / impl-retry / sonnet | 4 | 139 | 139-142 | 21 | 5 | 2.2 | 0.85 | 0.212 | 85.5% | 7688 |
| bench-final (ck0.9.0, 10-12) / impl-revise / haiku | 5 | 244 | 237-261 | 137 | 25 | 21.4 | 0.51 | 0.102 | 95.3% | 68899 |
| bench-final (ck0.9.0, 10-12) / impl-revise / opus | 2 | 120 | - | 110 | 22 | 3.7 | 1.74 | 0.872 | 95.3% | 16440 |
| bench-final (ck0.9.0, 10-12) / impl-revise / sonnet | 1 | 149 | - | 74 | 17 | 1.2 | 0.47 | 0.468 | 93.7% | 5767 |
| bench-final (ck0.9.0, 10-12) / land / haiku | 17 | 213 | 198-223 | 29 | 4 | 8.2 | 0.12 | 0.007 | 80.6% | 8277 |
| bench-final (ck0.9.0, 10-12) / orchestrator / sonnet | 1 | 142 | - | 4920 | 7 | 82.0 | 0.74 | 0.741 | 69.4% | 7485 |
| bench-final (ck0.9.0, 10-12) / plan / haiku | 61 | 228 | 219-249 | 5 | 2 | 5.5 | 0.36 | 0.006 | 62.7% | 22820 |
| bench-final (ck0.9.0, 10-12) / review / opus | 4 | 106 | 104-110 | 85 | 10 | 5.6 | 2.56 | 0.640 | 89.3% | 25091 |
| bench-final (ck0.9.0, 10-12) / review / sonnet | 37 | 125 | 118-130 | 64 | 9 | 41.7 | 14.18 | 0.383 | 88.4% | 211634 |
| bench-final (ck0.9.0, 10-12) / test / haiku | 11 | 259 | 251-269 | 303 | 45 | 85.4 | 4.01 | 0.365 | 96.4% | 298650 |
| bench-final (ck0.9.0, 10-12) / test-retry / haiku | 1 | 231 | - | 45 | 14 | 0.8 | 0.02 | 0.018 | 92.5% | 1729 |
| bench-final (ck0.9.0, 10-12) / test-retry / opus | 1 | 121 | - | 74 | 16 | 1.2 | 0.61 | 0.609 | 93.7% | 2228 |
| bench-final (ck0.9.0, 10-12) / test-retry / sonnet | 1 | 131 | - | 21 | 5 | 0.4 | 0.19 | 0.187 | 81.1% | 1767 |
| bench-final (ck0.9.0, 10-12) / test-revise / haiku | 8 | 241 | 235-261 | 104 | 22 | 34.5 | 0.75 | 0.094 | 94.2% | 98642 |
| bench-final (ck0.9.0, 10-12) / test-revise / sonnet | 1 | 101 | - | 44 | 9 | 0.7 | 0.28 | 0.278 | 87.5% | 2999 |
| bench-v2 (ck0.9.1, 10-12) / gate / haiku | 3 | 115 | - | 53 | 5 | 2.9 | 0.02 | 0.008 | 77.0% | 845 |
| bench-v2 (ck0.9.1, 10-12) / impl / haiku | 18 | 202 | 179-214 | 259 | 34 | 81.1 | 2.43 | 0.135 | 96.4% | 116180 |
| bench-v2 (ck0.9.1, 10-12) / impl / opus | 2 | 95 | - | 259 | 32 | 8.6 | 2.33 | 1.165 | 96.3% | 14197 |
| bench-v2 (ck0.9.1, 10-12) / impl-retry / haiku | 2 | 208 | - | 179 | 16 | 6.0 | 0.08 | 0.039 | 93.0% | 3423 |
| bench-v2 (ck0.9.1, 10-12) / impl-retry / opus | 1 | 122 | - | 80 | 14 | 1.3 | 0.45 | 0.447 | 93.2% | 2373 |
| bench-v2 (ck0.9.1, 10-12) / impl-retry / sonnet | 1 | 168 | - | 24 | 4 | 0.4 | 0.16 | 0.161 | 73.8% | 961 |
| bench-v2 (ck0.9.1, 10-12) / impl-revise / haiku | 5 | 199 | 195-230 | 163 | 24 | 14.2 | 0.36 | 0.071 | 95.3% | 14729 |
| bench-v2 (ck0.9.1, 10-12) / impl-revise / opus | 1 | 98 | - | 172 | 18 | 2.9 | 0.73 | 0.732 | 94.2% | 4646 |
| bench-v2 (ck0.9.1, 10-12) / impl-revise / sonnet | 1 | 71 | - | 90 | 13 | 1.5 | 0.33 | 0.332 | 91.2% | 1388 |
| bench-v2 (ck0.9.1, 10-12) / land / haiku | 24 | 200 | 186-210 | 30 | 4 | 25.7 | 0.16 | 0.007 | 79.0% | 12168 |
| bench-v2 (ck0.9.1, 10-12) / orchestrator / sonnet | 1 | 139 | - | 3221 | 6 | 53.7 | 0.46 | 0.464 | 81.7% | 7934 |
| bench-v2 (ck0.9.1, 10-12) / plan / haiku | 61 | 199 | 187-211 | 7 | 2 | 8.0 | 0.37 | 0.006 | 64.0% | 22913 |
| bench-v2 (ck0.9.1, 10-12) / review / opus | 3 | 102 | - | 132 | 9 | 5.1 | 1.98 | 0.660 | 87.7% | 22466 |
| bench-v2 (ck0.9.1, 10-12) / review / sonnet | 47 | 125 | 121-128 | 71 | 10 | 59.0 | 19.26 | 0.410 | 89.3% | 294473 |
| bench-v2 (ck0.9.1, 10-12) / test / haiku | 14 | 205 | 200-216 | 308 | 42 | 76.1 | 4.04 | 0.289 | 96.9% | 101958 |
| bench-v2 (ck0.9.1, 10-12) / test-revise / haiku | 8 | 219 | 214-254 | 130 | 24 | 29.4 | 0.95 | 0.119 | 95.0% | 25720 |
| bench-v2 (ck0.9.1, 10-12) / test-revise / sonnet | 4 | 134 | 133-144 | 64 | 14 | 4.1 | 1.38 | 0.346 | 92.1% | 9498 |
| escalation-pilot (10-10) / gate / haiku | 1 | 142 | - | 42 | 3 | 0.7 | 0.01 | 0.006 | 64.7% | 240 |
| escalation-pilot (10-10) / impl / haiku | 10 | 211 | 166-224 | 227 | 46 | 42.7 | 1.89 | 0.189 | 97.7% | 176515 |
| escalation-pilot (10-10) / impl-retry / haiku | 2 | 221 | - | 131 | 16 | 4.4 | 0.03 | 0.016 | 93.5% | 7943 |
| escalation-pilot (10-10) / impl-retry / opus | 2 | 117 | - | 241 | 22 | 8.0 | 1.45 | 0.726 | 95.7% | 9154 |
| escalation-pilot (10-10) / impl-retry / sonnet | 2 | 137 | - | 78 | 6 | 2.6 | 0.36 | 0.181 | 81.6% | 2842 |
| escalation-pilot (10-10) / impl-revise / haiku | 5 | 239 | 228-253 | 132 | 27 | 11.5 | 0.23 | 0.046 | 96.0% | 69796 |
| escalation-pilot (10-10) / impl-revise / opus | 1 | 120 | - | 160 | 22 | 2.7 | 0.73 | 0.734 | 95.0% | 7113 |
| escalation-pilot (10-10) / impl-revise / sonnet | 3 | 140 | - | 71 | 11 | 3.4 | 0.63 | 0.210 | 89.6% | 3923 |
| escalation-pilot (10-10) / land / haiku | 8 | 131 | 123-138 | 30 | 11 | 4.4 | 0.07 | 0.009 | 91.0% | 4394 |
| escalation-pilot (10-10) / orchestrator / sonnet | 1 | 130 | - | 2257 | 7 | 37.6 | 0.38 | 0.381 | 84.5% | 5595 |
| escalation-pilot (10-10) / plan / haiku | 39 | 190 | 173-208 | 6 | 2 | 4.9 | 0.20 | 0.005 | 60.1% | 17635 |
| escalation-pilot (10-10) / review / opus | 26 | 104 | 101-106 | 118 | 13 | 54.7 | 18.72 | 0.720 | 92.6% | 147302 |
| escalation-pilot (10-10) / test / haiku | 2 | 224 | - | 457 | 50 | 15.2 | 0.84 | 0.420 | 97.5% | 82511 |
| escalation-pilot (10-10) / test-retry / haiku | 1 | 168 | - | 66 | 14 | 1.1 | 0.02 | 0.015 | 92.4% | 2234 |
| escalation-pilot (10-10) / test-retry / opus | 1 | 116 | - | 128 | 20 | 2.1 | 0.81 | 0.805 | 94.0% | 6192 |
| escalation-pilot (10-10) / test-retry / sonnet | 1 | 129 | - | 33 | 6 | 0.6 | 0.21 | 0.210 | 80.9% | 1832 |
| escalation-pilot (10-10) / test-revise / haiku | 2 | 241 | - | 239 | 34 | 8.0 | 0.23 | 0.113 | 96.8% | 31786 |
| escalation-pilot (10-10) / test-revise / opus | 2 | 129 | - | 160 | 22 | 5.3 | 1.83 | 0.916 | 95.0% | 19222 |
| escalation-pilot (10-10) / test-revise / sonnet | 2 | 146 | - | 190 | 20 | 6.3 | 0.92 | 0.459 | 94.4% | 6342 |

## Per model (all roles)
| key | n | median tok/s | p25-p75 tok/s | median wall s | median turns | sum wall min | sum $ | $/agent | cache-read share of input | output tok |
|---|---|---|---|---|---|---|---|---|---|---|
| haiku | 406 | 212 | 192-230 | 19 | 4 | 687.8 | 23.42 | 0.058 | 94.3% | 1553337 |
| opus | 328 | 99 | 91-104 | 111 | 7 | 2107.2 | 188.37 | 0.574 | 90.4% | 800803 |
| sonnet | 241 | 136 | 126-150 | 81 | 11 | 866.7 | 102.84 | 0.427 | 91.8% | 735046 |

## Cost and token totals per run
| run | agents | sum $ (transcript) | of which orchestrator main | transcript tokens (in+out+cr+cw) | workflowProgress tokens (sum per agent) | output file totalTokens | wp/transcript |
|---|---|---|---|---|---|---|---|
| bench-v2 (ck0.9.1, 10-12) | 195 | 35.50 | 0.46 | 222.2M | 15.6M | 15.6M | 0.07 |
| bench-final (ck0.9.0, 10-12) | 180 | 37.17 | 0.74 | 207.5M | 14.1M | 14.1M | 0.07 |
| escalation-pilot (10-10) | 110 | 29.56 | 0.38 | 128.3M | 7.6M | 7.6M | 0.06 |
| all-haiku (10-10) | 78 | 2.84 | 0.05 | 74.6M | 5.9M | 5.9M | 0.08 |
| baseline sonnet+opus (10-10..12) | 399 | 209.55 | 24.74 | 322.9M | 29.0M | 29.0M | 0.09 |

## Where time goes (agent wall-clock seconds, summed; and serial share = union of intervals)
| run | class | n | agent-min | share of agent-min | union-min (wall covered) | share of run wall | $ | share of $ |
|---|---|---|---|---|---|---|---|---|
| bench-v2 (ck0.9.1, 10-12) | implementation (incl. retries/revisions) | 57 | 226 | 69% | 50 | 93% | 13.24 | 38% |
| bench-v2 (ck0.9.1, 10-12) | mechanical: gate | 3 | 3 | 1% | 3 | 5% | 0.02 | 0% |
| bench-v2 (ck0.9.1, 10-12) | mechanical: land | 24 | 26 | 8% | 26 | 48% | 0.16 | 0% |
| bench-v2 (ck0.9.1, 10-12) | mechanical: plan | 61 | 8 | 2% | 8 | 15% | 0.37 | 1% |
| bench-v2 (ck0.9.1, 10-12) | review | 50 | 64 | 20% | 34 | 63% | 21.24 | 61% |
| bench-v2 (ck0.9.1, 10-12) | (run wall) | | | | 54 | | | |
| bench-final (ck0.9.0, 10-12) | implementation (incl. retries/revisions) | 59 | 226 | 69% | 50 | 61% | 19.18 | 53% |
| bench-final (ck0.9.0, 10-12) | mechanical: gate | 2 | 41 | 12% | 41 | 50% | 0.04 | 0% |
| bench-final (ck0.9.0, 10-12) | mechanical: land | 17 | 8 | 2% | 8 | 10% | 0.12 | 0% |
| bench-final (ck0.9.0, 10-12) | mechanical: plan | 61 | 5 | 2% | 5 | 7% | 0.36 | 1% |
| bench-final (ck0.9.0, 10-12) | review | 41 | 47 | 14% | 29 | 35% | 16.74 | 46% |
| bench-final (ck0.9.0, 10-12) | (run wall) | | | | 82 | | | |
| escalation-pilot (10-10) | implementation (incl. retries/revisions) | 36 | 114 | 64% | 31 | 82% | 10.18 | 35% |
| escalation-pilot (10-10) | mechanical: gate | 1 | 1 | 0% | 1 | 2% | 0.01 | 0% |
| escalation-pilot (10-10) | mechanical: land | 8 | 4 | 2% | 4 | 12% | 0.07 | 0% |
| escalation-pilot (10-10) | mechanical: plan | 39 | 5 | 3% | 5 | 13% | 0.20 | 1% |
| escalation-pilot (10-10) | review | 26 | 55 | 31% | 28 | 73% | 18.72 | 64% |
| escalation-pilot (10-10) | (run wall) | | | | 38 | | | |
| all-haiku (10-10) | implementation (incl. retries/revisions) | 19 | 56 | 67% | 19 | 82% | 1.97 | 70% |
| all-haiku (10-10) | mechanical: gate | 1 | 1 | 1% | 1 | 5% | 0.01 | 0% |
| all-haiku (10-10) | mechanical: land | 9 | 3 | 3% | 3 | 12% | 0.08 | 3% |
| all-haiku (10-10) | mechanical: plan | 32 | 3 | 3% | 3 | 12% | 0.21 | 7% |
| all-haiku (10-10) | review | 17 | 21 | 25% | 15 | 67% | 0.53 | 19% |
| all-haiku (10-10) | (run wall) | | | | 23 | | | |
| baseline sonnet+opus (10-10..12) | fix/resolve/other | 2 | 39 | 2% | 39 | 4% | 1.01 | 1% |
| baseline sonnet+opus (10-10..12) | implementation (incl. retries/revisions) | 132 | 559 | 34% | 327 | 37% | 61.87 | 33% |
| baseline sonnet+opus (10-10..12) | mechanical: gate | 12 | 154 | 9% | 154 | 17% | 6.64 | 4% |
| baseline sonnet+opus (10-10..12) | mechanical: land | 56 | 184 | 11% | 184 | 21% | 15.36 | 8% |
| baseline sonnet+opus (10-10..12) | mechanical: plan | 85 | 208 | 13% | 208 | 23% | 22.55 | 12% |
| baseline sonnet+opus (10-10..12) | review | 112 | 511 | 31% | 316 | 35% | 77.37 | 42% |
| baseline sonnet+opus (10-10..12) | (run wall) | | | | 896 | | | |

## Implementation outcomes per run (task-level)
Definitions: first-attempt success = the first impl/test agent for the task reported gatePassed and the first review approved (no retry, no revision, no escalation). Path-labelled tasks (".ck/worktrees/...-task-N") are folded into task N.

| run | first model | kind | tasks | first-attempt success | first gate green | first review approve | escalated | merged | median impl+fix $ per task | median review $ per task |
|---|---|---|---|---|---|---|---|---|---|---|
| all-haiku (10-10) | haiku | impl | 11 | 7 (64%) | 10 | 7/10 | 0 | 9 | 0.07 | 0.02 |
| all-haiku (10-10) | haiku | test | 2 | 0 (0%) | 2 | 0/2 | 0 | 0 | 0.40 | 0.15 |
| baseline sonnet+opus (10-10..12) | sonnet | impl | 28 | 10 (36%) | 21 | 17/28 | 0 | 28 | 0.74 | 0.70 |
| baseline sonnet+opus (10-10..12) | sonnet | test | 30 | 10 (33%) | 28 | 10/30 | 0 | 28 | 0.89 | 1.23 |
| bench-final (ck0.9.0, 10-12) | haiku | impl | 13 | 5 (38%) | 9 | 5/10 | 5 | 6 | 0.39 | 0.42 |
| bench-final (ck0.9.0, 10-12) | haiku | test | 11 | 3 (27%) | 10 | 3/11 | 1 | 3 | 0.45 | 0.69 |
| bench-final (ck0.9.0, 10-12) | opus | impl | 3 | 2 (67%) | 3 | 2/3 | 0 | 2 | 1.26 | 0.63 |
| bench-v2 (ck0.9.1, 10-12) | haiku | impl | 16 | 9 (56%) | 14 | 10/15 | 2 | 13 | 0.21 | 0.28 |
| bench-v2 (ck0.9.1, 10-12) | haiku | test | 13 | 5 (38%) | 13 | 5/13 | 4 | 13 | 0.37 | 0.74 |
| bench-v2 (ck0.9.1, 10-12) | opus | impl | 2 | 1 (50%) | 2 | 1/2 | 0 | 1 | 1.53 | 0.99 |
| escalation-pilot (10-10) | haiku | impl | 10 | 4 (40%) | 8 | 4/9 | 4 | 8 | 0.16 | 0.66 |
| escalation-pilot (10-10) | haiku | test | 2 | 0 (0%) | 1 | 0/2 | 2 | 0 | 2.42 | 3.78 |

## Mechanical-agent result health
| run | role | model | n | ok | notes |
|---|---|---|---|---|---|
| bench-v2 (ck0.9.1, 10-12) | plan | haiku | 61 | 54 | ready[] contained worktree paths instead of task ids x7 |
| bench-v2 (ck0.9.1, 10-12) | land | haiku | 24 | 22 | state MERGED x27; state MISSING x2 |
| bench-v2 (ck0.9.1, 10-12) | gate | haiku | 3 | 3 |  |
| bench-final (ck0.9.0, 10-12) | plan | haiku | 61 | 58 | ready[] contained worktree paths instead of task ids x3 |
| bench-final (ck0.9.0, 10-12) | land | haiku | 17 | 16 | state MERGED x11; state MISSING x1 |
| bench-final (ck0.9.0, 10-12) | gate | haiku | 2 | 2 |  |
| escalation-pilot (10-10) | plan | haiku | 39 | 39 |  |
| escalation-pilot (10-10) | land | haiku | 8 | 0 | no result x8 |
| escalation-pilot (10-10) | gate | haiku | 1 | 1 |  |
| all-haiku (10-10) | plan | haiku | 32 | 32 |  |
| all-haiku (10-10) | land | haiku | 9 | 0 | no result x9 |
| all-haiku (10-10) | gate | haiku | 1 | 1 |  |
| baseline sonnet+opus (10-10..12) | plan | opus | 85 | 85 |  |
| baseline sonnet+opus (10-10..12) | land | opus | 56 | 0 | no result x56 |
| baseline sonnet+opus (10-10..12) | gate | opus | 12 | 11 | not green / no result x1 |
